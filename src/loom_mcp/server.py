import asyncio
import json
import os
import re
from pathlib import Path
from typing import Annotated

from fastmcp import FastMCP, Context
from fastmcp.exceptions import ToolError
from fastmcp.server.lifespan import lifespan
from pydantic import Field

from loom_mcp.client import LoomClient, LoomAPIError

_ID_RE = re.compile(r"\A[a-zA-Z0-9_.\-]{1,200}\Z")


def _find_project_root() -> Path | None:
    """Walk up from this file to find the directory containing pyproject.toml."""
    d = Path(__file__).resolve().parent
    while d != d.parent:
        if (d / "pyproject.toml").exists():
            return d
        d = d.parent
    return None


# In a local dev checkout (loom-api/mcp-server/), the project root's parent
# contains .env and auth.json.  When installed via uvx, _find_project_root()
# returns None and these paths are skipped gracefully.
_PROJECT_ROOT = _find_project_root()

if _PROJECT_ROOT is not None:
    _env_path = _PROJECT_ROOT.parent / ".env"
    if _env_path.exists():
        for _line in _env_path.read_text().splitlines():
            if _line.strip() and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip().strip("'\""))


@lifespan
async def app_lifespan(server):
    cookie = os.environ.get("LOOM_COOKIE", "")
    if cookie and not cookie.startswith("connect.sid="):
        cookie = f"connect.sid={cookie}"
    if cookie:
        client = LoomClient(cookies=cookie)
    else:
        auth_file = os.environ.get("LOOM_AUTH_FILE")
        if not auth_file and _PROJECT_ROOT is not None:
            auth_file = str(_PROJECT_ROOT.parent / "auth.json")
        if not auth_file:
            raise ValueError(
                "Set LOOM_COOKIE or LOOM_AUTH_FILE. "
                "See https://github.com/karbassi/loom-mcp#authentication"
            )
        client = LoomClient(auth_file=auth_file)

    try:
        yield {"loom": client}
    finally:
        await client.aclose()


mcp = FastMCP(
    "Loom",
    instructions="Access Loom videos, transcripts, summaries, and comments.",
    lifespan=app_lifespan,
    version="1.1.0",
)


def _get_client(ctx: Context) -> LoomClient:
    return ctx.lifespan_context["loom"]


def _save(
    save_dir: str | None, video_id: str, filename: str, content: str
) -> Path | None:
    if not save_dir or not content:
        return None
    video_dir = Path(save_dir).expanduser().resolve() / video_id
    video_dir.mkdir(parents=True, exist_ok=True)
    path = video_dir / filename
    path.write_text(content, encoding="utf-8")
    return path


def _with_saved(text: str, path: Path | None) -> str:
    if path:
        return f"{text}\n\n[Saved to {path}]"
    return text


async def _call(coro):
    """Await a client coroutine, converting LoomAPIError to ToolError."""
    try:
        return await coro
    except LoomAPIError as e:
        raise ToolError(str(e)) from None


def _id(value: str, label: str = "ID") -> str:
    """Validate a single resource ID."""
    if not _ID_RE.fullmatch(value):
        raise ToolError(f"Invalid {label}: {value!r}")
    return value


def _ids(values: list[str], label: str = "ID") -> list[str]:
    """Validate a list of resource IDs."""
    for v in values:
        _id(v, label)
    return values


# ---------------------------------------------------------------------------
# Read Tools
# ---------------------------------------------------------------------------


@mcp.tool(
    tags={"read"},
    timeout=30.0,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def list_videos(
    ctx: Context,
    limit: Annotated[
        int, Field(description="Max videos to return (default 50)", ge=1, le=200)
    ] = 50,
) -> str:
    """List your Loom videos, sorted by most recent.

    Returns video IDs and names. Use get_video for metadata on a specific video,
    or get_video_details for comprehensive info including transcript and comments.
    """
    client = _get_client(ctx)
    videos = []
    cursor = None
    while len(videos) < limit:
        batch_size = min(50, limit - len(videos))
        result = await _call(client.list_videos(limit=batch_size, cursor=cursor))
        videos.extend(result["videos"])
        if not result["hasNextPage"]:
            break
        cursor = result["endCursor"]
    lines = [f"{v['id']}  {v['name']}" for v in videos]
    return f"Found {len(videos)} videos:\n\n" + "\n".join(lines)


@mcp.tool(
    tags={"read"},
    timeout=30.0,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def search_videos(
    ctx: Context,
    query: Annotated[str, "Search query"],
    limit: Annotated[
        int, Field(description="Max results to return (default 50)", ge=1, le=200)
    ] = 50,
) -> str:
    """Search Loom videos by keyword. Returns matching video IDs and names."""
    client = _get_client(ctx)
    videos = []
    cursor = None
    while len(videos) < limit:
        batch_size = min(50, limit - len(videos))
        result = await _call(
            client.search_videos(query, limit=batch_size, cursor=cursor)
        )
        videos.extend(result["videos"])
        if not result["hasNextPage"]:
            break
        cursor = result["endCursor"]
    if not videos:
        return f"No videos matching '{query}'"
    lines = [f"{v['id']}  {v['name']}" for v in videos]
    return f"Found {len(videos)} matching videos:\n\n" + "\n".join(lines)


@mcp.tool(
    tags={"read"},
    timeout=30.0,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def get_video(
    ctx: Context,
    video_id: Annotated[str, "The Loom video ID (32-char hex string)"],
    save_dir: Annotated[
        str | None, "Directory to save output to (omit to skip saving)"
    ] = None,
) -> str:
    """Get metadata for a Loom video including name, duration, owner, views, and creation date.

    For comprehensive info including transcript, chapters, summary, and comments in one call,
    use get_video_details instead.
    """
    client = _get_client(ctx)
    vid = _id(video_id, "video ID")
    video = await _call(client.get_video(vid))
    if video.get("message"):
        raise ToolError(f"Cannot access video {vid}: {video['message']}")
    text = json.dumps(video, indent=2)
    path = _save(save_dir, vid, "metadata.json", text)
    return _with_saved(text, path)


@mcp.tool(
    tags={"read"},
    timeout=30.0,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def get_transcript(
    ctx: Context,
    video_id: Annotated[str, "The Loom video ID"],
    save_dir: Annotated[
        str | None, "Directory to save output to (omit to skip saving)"
    ] = None,
) -> str:
    """Get the full transcript of a Loom video with timestamps and speaker names.

    Returns plain text with one line per phrase. For precise start/end timing per cue
    in WebVTT format, use get_captions instead.
    """
    client = _get_client(ctx)
    vid = _id(video_id, "video ID")
    text = await _call(client.get_transcript_text(vid))
    if not text:
        return "No transcript available for this video."
    path = _save(save_dir, vid, "transcript.txt", text)
    return _with_saved(text, path)


@mcp.tool(
    tags={"read"},
    timeout=30.0,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def get_captions(
    ctx: Context,
    video_id: Annotated[str, "The Loom video ID"],
    save_dir: Annotated[
        str | None, "Directory to save output to (omit to skip saving)"
    ] = None,
) -> str:
    """Get WebVTT captions of a Loom video with start and end timestamps per cue.

    Ideal for precise timing analysis. For plain text with speaker names, use get_transcript instead.
    """
    client = _get_client(ctx)
    vid = _id(video_id, "video ID")
    vtt = await _call(client.get_captions(vid))
    if not vtt:
        return "No captions available for this video."
    path = _save(save_dir, vid, "captions.vtt", vtt)
    return _with_saved(vtt, path)


@mcp.tool(
    tags={"read"},
    timeout=30.0,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def get_summary(
    ctx: Context,
    video_id: Annotated[str, "The Loom video ID"],
    save_dir: Annotated[
        str | None, "Directory to save output to (omit to skip saving)"
    ] = None,
) -> str:
    """Get the AI-generated summary of a Loom video (1-2 concise sentences).

    For a detailed timestamped breakdown with bullet points, use get_description.
    For key highlights as a bullet list, use get_key_takeaways.
    For chapter markers, use get_chapters.
    """
    client = _get_client(ctx)
    vid = _id(video_id, "video ID")
    summary = await _call(client.get_summary(vid))
    if not summary or not summary.get("autoDescription"):
        return "No AI summary available for this video."
    text = summary["autoDescription"]
    path = _save(save_dir, vid, "summary.txt", text)
    return _with_saved(text, path)


@mcp.tool(
    tags={"read"},
    timeout=30.0,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def get_chapters(
    ctx: Context,
    video_id: Annotated[str, "The Loom video ID"],
    save_dir: Annotated[
        str | None, "Directory to save output to (omit to skip saving)"
    ] = None,
) -> str:
    """Get AI-generated chapter markers with timestamps for a Loom video.

    Useful for navigating long videos. For a narrative summary, use get_summary.
    """
    client = _get_client(ctx)
    vid = _id(video_id, "video ID")
    chapters = await _call(client.get_chapters(vid))
    if not chapters or not chapters.get("content"):
        return "No chapters available for this video."
    text = chapters["content"]
    path = _save(save_dir, vid, "chapters.txt", text)
    return _with_saved(text, path)


@mcp.tool(
    tags={"read"},
    timeout=30.0,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def get_comments(
    ctx: Context,
    video_id: Annotated[str, "The Loom video ID"],
    save_dir: Annotated[
        str | None, "Directory to save output to (omit to skip saving)"
    ] = None,
) -> str:
    """Get comments on a Loom video, including threaded replies and timestamps."""
    client = _get_client(ctx)
    vid = _id(video_id, "video ID")
    comments = await _call(client.get_comments(vid))
    if not comments:
        return "No comments on this video."
    lines = []
    for c in comments:
        ts = f" @{c['time_stamp']}s" if c.get("time_stamp") is not None else ""
        lines.append(f"[{c['user_name']}{ts}] {c['content']}")
        for r in c.get("children_comments") or []:
            lines.append(f"  └─ [{r['user_name']}] {r['content']}")
    text = "\n".join(lines)
    path = _save(save_dir, vid, "comments.txt", text)
    return _with_saved(text, path)


@mcp.tool(
    tags={"read"},
    timeout=30.0,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def get_download_url(
    ctx: Context,
    video_id: Annotated[str, "The Loom video ID"],
) -> str:
    """Get a signed download URL for the MP4 file of a Loom video. The URL is temporary and will expire. If no URL is available, call regenerate_mp4 then retry this tool after ~30 seconds."""
    client = _get_client(ctx)
    url = await _call(client.get_download_url(_id(video_id, "video ID")))
    if not url:
        return "No download URL available. Call regenerate_mp4 for this video, then retry get_download_url after ~30 seconds."
    return url


@mcp.tool(
    tags={"read"},
    timeout=600.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def download_media(
    ctx: Context,
    video_id: Annotated[str, "The Loom video ID"],
    out_path: Annotated[
        str,
        "Local output path; the extension sets the container (.wav/.m4a/.mp3 for audio, .mp4/.mkv/.webm for video)",
    ],
    kind: Annotated[str, "'audio' (default) or 'video'"] = "audio",
    quality: Annotated[
        str,
        "Video only: 'best' (highest bitrate, e.g. 1080p) or 'small' (lowest, e.g. 720p)",
    ] = "best",
    start: Annotated[float | None, "Trim start, in seconds from the beginning"] = None,
    end: Annotated[float | None, "Trim end, in seconds from the beginning"] = None,
) -> str:
    """Download a Loom video's audio or video to a local file. Works even when MP4 export is disabled (e.g. notetaker recordings where get_download_url returns nothing). Optionally trim to a [start, end] range in seconds; only the needed segments are fetched. .mp4/.wav/.m4a/.mp3 outputs are re-encoded for frame-accurate trims; .webm/.mkv are stream-copied (fast, but trim points snap to keyframes). Requires ffmpeg on PATH."""
    if kind not in ("audio", "video"):
        raise ToolError("kind must be 'audio' or 'video'")
    if quality not in ("best", "small"):
        raise ToolError("quality must be 'best' or 'small'")
    if start is not None and start < 0:
        raise ToolError("start must be >= 0")
    if start is not None and end is not None and end <= start:
        raise ToolError("end must be greater than start")
    client = _get_client(ctx)
    path = await _call(
        client.download_media(
            _id(video_id, "video ID"),
            out_path,
            kind=kind,
            quality=quality,
            start=start,
            end=end,
        )
    )
    return f"Saved {kind} to {path}"


@mcp.tool(
    tags={"read"},
    timeout=30.0,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def get_tasks(
    ctx: Context,
    video_id: Annotated[str, "The Loom video ID"],
    save_dir: Annotated[
        str | None, "Directory to save output to (omit to skip saving)"
    ] = None,
) -> str:
    """Get AI-generated action items (tasks) from a Loom video, including assignee, status, and timestamp."""
    client = _get_client(ctx)
    vid = _id(video_id, "video ID")
    tasks = await _call(client.get_tasks(vid))
    if not tasks:
        return "No tasks/action items for this video."
    lines = []
    for t in tasks:
        owner = (t.get("owner") or {}).get("display_name", "Unassigned")
        ts = f" @{t['time_stamp']}s" if t.get("time_stamp") is not None else ""
        status = "resolved" if t.get("resolved_at") else "open"
        lines.append(f"[{status}] [{owner}{ts}] {t['content']}")
    text = "\n".join(lines)
    path = _save(save_dir, vid, "tasks.txt", text)
    return _with_saved(text, path)


@mcp.tool(
    tags={"read"},
    timeout=30.0,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def get_reactions(
    ctx: Context,
    video_id: Annotated[str, "The Loom video ID"],
    save_dir: Annotated[
        str | None, "Directory to save output to (omit to skip saving)"
    ] = None,
) -> str:
    """Get emoji reactions on a Loom video, including who reacted and at what timestamp."""
    client = _get_client(ctx)
    vid = _id(video_id, "video ID")
    reactions = await _call(client.get_reactions(vid))
    if not reactions:
        return "No reactions on this video."
    lines = []
    for r in reactions:
        user = (r.get("user") or {}).get("display_name") or r.get(
            "anon_user_name", "Anonymous"
        )
        emoji = r.get("extended_reaction") or r.get("reaction", "")
        ts = f" @{r['time']}s" if r.get("time") is not None else ""
        lines.append(f"[{user}{ts}] {emoji}")
    text = "\n".join(lines)
    path = _save(save_dir, vid, "reactions.txt", text)
    return _with_saved(text, path)


@mcp.tool(
    tags={"read"},
    timeout=30.0,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def get_meeting_notes(
    ctx: Context,
    video_id: Annotated[str, "The Loom video ID"],
) -> str:
    """Get the Confluence meeting notes URL linked to a Loom video."""
    client = _get_client(ctx)
    url = await _call(client.get_meeting_notes_url(_id(video_id, "video ID")))
    if not url:
        return "No meeting notes linked to this video."
    return url


@mcp.tool(
    tags={"read"},
    timeout=30.0,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def list_folders(
    ctx: Context,
    limit: Annotated[
        int, Field(description="Max folders to return (default 50)", ge=1, le=200)
    ] = 50,
) -> str:
    """List your Loom folders, sorted by most recent."""
    client = _get_client(ctx)
    folders = []
    cursor = None
    while len(folders) < limit:
        batch_size = min(50, limit - len(folders))
        result = await _call(client.list_folders(limit=batch_size, cursor=cursor))
        folders.extend(result["folders"])
        if not result["hasNextPage"]:
            break
        cursor = result["endCursor"]
    if not folders:
        return "No folders found."
    lines = [
        f"{f['id']}  {f['name']}  ({f.get('visibility', 'unknown')})" for f in folders
    ]
    return f"Found {len(folders)} folders:\n\n" + "\n".join(lines)


@mcp.tool(
    tags={"read"},
    timeout=30.0,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def list_spaces(ctx: Context) -> str:
    """List your Loom spaces (workspaces)."""
    client = _get_client(ctx)
    spaces = []
    cursor = None
    while True:
        result = await _call(client.list_spaces(limit=50, cursor=cursor))
        spaces.extend(result["spaces"])
        if not result["hasNextPage"]:
            break
        cursor = result["endCursor"]
    if not spaces:
        return "No spaces found."
    lines = []
    for s in spaces:
        primary = " (primary)" if s.get("is_primary") else ""
        privacy = s.get("privacy") or "unknown"
        lines.append(f"{s['id']}  {s['name']}  [{privacy}]{primary}")
    return f"Found {len(spaces)} spaces:\n\n" + "\n".join(lines)


@mcp.tool(
    tags={"read"},
    timeout=30.0,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def get_space(
    ctx: Context,
    space_id: Annotated[str, "The Loom space ID"],
) -> str:
    """Get details of a Loom space including name, privacy level, and whether it's the primary space."""
    client = _get_client(ctx)
    space = await _call(client.get_space(_id(space_id, "space ID")))
    if not space:
        raise ToolError(f"Space not found: {space_id}")
    return json.dumps(space, indent=2)


@mcp.tool(
    tags={"read"},
    timeout=30.0,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def get_backlinks(
    ctx: Context,
    video_id: Annotated[str, "The Loom video ID"],
    save_dir: Annotated[
        str | None, "Directory to save output to (omit to skip saving)"
    ] = None,
) -> str:
    """Get external references (backlinks) to a Loom video — where it's been shared or embedded."""
    client = _get_client(ctx)
    vid = _id(video_id, "video ID")
    backlinks = await _call(client.get_backlinks(vid))
    if not backlinks:
        return "No backlinks for this video."
    lines = []
    for b in backlinks:
        source = b.get("source", "unknown")
        title = b.get("title", "Untitled")
        link = b.get("sourceLink", "")
        lines.append(f"[{source}] {title} — {link}")
    text = "\n".join(lines)
    path = _save(save_dir, vid, "backlinks.txt", text)
    return _with_saved(text, path)


@mcp.tool(
    tags={"read"},
    timeout=30.0,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def get_key_takeaways(
    ctx: Context,
    video_id: Annotated[str, "The Loom video ID"],
    save_dir: Annotated[
        str | None, "Directory to save output to (omit to skip saving)"
    ] = None,
) -> str:
    """Get AI-generated key takeaways from a Loom video as a bullet list.

    For a narrative summary, use get_summary. For a detailed timestamped breakdown, use get_description.
    """
    client = _get_client(ctx)
    vid = _id(video_id, "video ID")
    takeaways = await _call(client.get_key_takeaways(vid))
    if not takeaways:
        return "No key takeaways available for this video."
    text = "\n".join(f"- {t}" for t in takeaways)
    path = _save(save_dir, vid, "takeaways.txt", text)
    return _with_saved(text, path)


@mcp.tool(
    tags={"read"},
    timeout=30.0,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def get_tags(
    ctx: Context,
    video_id: Annotated[str, "The Loom video ID"],
    save_dir: Annotated[
        str | None, "Directory to save output to (omit to skip saving)"
    ] = None,
) -> str:
    """Get tags on a Loom video."""
    client = _get_client(ctx)
    vid = _id(video_id, "video ID")
    tags = await _call(client.get_tags(vid))
    if not tags:
        return "No tags on this video."
    text = ", ".join(tags)
    path = _save(save_dir, vid, "tags.txt", text)
    return _with_saved(text, path)


@mcp.tool(
    tags={"read"},
    timeout=30.0,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def get_description(
    ctx: Context,
    video_id: Annotated[str, "The Loom video ID"],
    save_dir: Annotated[
        str | None, "Directory to save output to (omit to skip saving)"
    ] = None,
) -> str:
    """Get the AI-generated description of a Loom video with timestamped sections and bullet points.

    More detailed than get_summary. For a brief 1-2 sentence summary, use get_summary instead.
    """
    client = _get_client(ctx)
    vid = _id(video_id, "video ID")
    desc = await _call(client.get_description(vid))
    if not desc:
        return "No description available for this video."
    path = _save(save_dir, vid, "description.txt", desc)
    return _with_saved(desc, path)


@mcp.tool(
    tags={"read"},
    timeout=30.0,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def get_confluence_pages(
    ctx: Context,
    video_id: Annotated[str, "The Loom video ID"],
) -> str:
    """Get Confluence pages linked to a Loom video."""
    client = _get_client(ctx)
    pages = await _call(client.get_confluence_pages(_id(video_id, "video ID")))
    if not pages:
        return "No Confluence pages linked to this video."
    lines = [f"- [{p.get('title', 'Untitled')}]({p.get('url', '')})" for p in pages]
    return "\n".join(lines)


@mcp.tool(
    tags={"read"},
    timeout=30.0,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def search_folders(
    ctx: Context,
    query: Annotated[str, "Search query for folders"],
) -> str:
    """Search your Loom folders by name."""
    client = _get_client(ctx)
    folders = await _call(client.search_folders(query))
    if not folders:
        return f"No folders matching '{query}'"
    lines = [f"{f['id']}  {f['name']}" for f in folders]
    return f"Found {len(folders)} folders:\n\n" + "\n".join(lines)


@mcp.tool(
    tags={"read"},
    timeout=30.0,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def get_last_watch_time(
    ctx: Context,
    video_id: Annotated[str, "The Loom video ID"],
) -> str:
    """Get the last timestamp (in seconds) where you stopped watching a Loom video."""
    client = _get_client(ctx)
    time = await _call(client.get_last_watch_time(_id(video_id, "video ID")))
    if time is None:
        return "No watch history for this video."
    return f"Last watched at {time}s"


@mcp.tool(
    tags={"read"},
    timeout=30.0,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def get_watch_later_count(ctx: Context) -> str:
    """Get the number of videos in your Watch Later list."""
    client = _get_client(ctx)
    count = await _call(client.get_watch_later_count())
    return f"Watch Later list has {count} videos"


@mcp.tool(
    tags={"read"},
    timeout=30.0,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def get_total_videos_count(
    ctx: Context,
    user_id: Annotated[str, "The Loom user ID"],
) -> str:
    """Get the total number of videos created by a user."""
    client = _get_client(ctx)
    count = await _call(client.get_total_videos_count(_id(user_id, "user ID")))
    return f"User has {count} videos"


@mcp.tool(
    tags={"read"},
    timeout=30.0,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def get_frequent_reactions(ctx: Context) -> str:
    """Get your most frequently used emoji reaction types.

    Also useful to discover valid reaction type values for add_reaction.
    """
    client = _get_client(ctx)
    reactions = await _call(client.get_frequent_reactions())
    if not reactions:
        return "No recent reactions."
    return "Your frequent reactions: " + ", ".join(reactions)


@mcp.tool(
    tags={"read"},
    timeout=30.0,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def get_comment_reactions(
    ctx: Context,
    comment_id: Annotated[str, "The comment ID"],
    comment_type: Annotated[str, "Comment type: COMMENT or REPLY"] = "COMMENT",
) -> str:
    """Get emoji reactions on a specific comment."""
    client = _get_client(ctx)
    reactions = await _call(
        client.get_comment_reactions(_id(comment_id, "comment ID"), comment_type)
    )
    if not reactions:
        return "No reactions on this comment."
    lines = []
    for r in reactions:
        emoji = r.get("extendedReaction", "")
        user = r.get("userName", "Unknown")
        lines.append(f"[{user}] {emoji}")
    return "\n".join(lines)


@mcp.tool(
    tags={"read"},
    timeout=30.0,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def get_video_details(
    ctx: Context,
    video_id: Annotated[str, "The Loom video ID"],
    save_dir: Annotated[
        str | None, "Directory to save output to (omit to skip saving)"
    ] = None,
) -> str:
    """Get all available information for a Loom video in one call: metadata, transcript, chapters, summary, tasks, and comments.

    Use this when you need a complete picture of a video. For just metadata, use get_video.
    """
    vid = _id(video_id, "video ID")
    client = _get_client(ctx)
    video = await _call(client.get_video(vid))
    if video.get("message"):
        raise ToolError(f"Cannot access video {vid}: {video['message']}")
    transcript, chapters, summary, comments, tasks = await asyncio.gather(
        _call(client.get_transcript_text(vid)),
        _call(client.get_chapters(vid)),
        _call(client.get_summary(vid)),
        _call(client.get_comments(vid)),
        _call(client.get_tasks(vid)),
    )

    # Save individual pieces
    _save(save_dir, vid, "metadata.json", json.dumps(video, indent=2))

    parts = [f"# {video.get('name', 'Unknown')}\n"]

    duration = video.get("playable_duration") or 0
    m, s = divmod(int(duration), 60)
    h, m = divmod(m, 60)
    dur_str = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"
    parts.append(f"**Duration:** {dur_str}")
    parts.append(f"**Created:** {video.get('createdAt', 'Unknown')}")
    parts.append(
        f"**Owner:** {(video.get('owner') or {}).get('display_name', 'Unknown')}"
    )
    parts.append(f"**Views:** {(video.get('views') or {}).get('total', 0)}")
    parts.append("")

    if chapters and chapters.get("content"):
        _save(save_dir, vid, "chapters.txt", chapters["content"])
        parts.append("## Chapters\n")
        parts.append(chapters["content"])
        parts.append("")

    if summary and summary.get("autoDescription"):
        _save(save_dir, vid, "summary.txt", summary["autoDescription"])
        parts.append("## AI Summary\n")
        parts.append(summary["autoDescription"])
        parts.append("")

    if transcript:
        _save(save_dir, vid, "transcript.txt", transcript)
        parts.append("## Transcript\n")
        parts.append(transcript)
        parts.append("")

    if tasks:
        task_lines = []
        for t in tasks:
            owner = (t.get("owner") or {}).get("display_name", "Unassigned")
            ts = f" @{t['time_stamp']}s" if t.get("time_stamp") is not None else ""
            status = "resolved" if t.get("resolved_at") else "open"
            task_lines.append(f"[{status}] [{owner}{ts}] {t['content']}")
        _save(save_dir, vid, "tasks.txt", "\n".join(task_lines))
        parts.append("## Action Items\n")
        for line in task_lines:
            parts.append(f"- {line}")
        parts.append("")

    if comments:
        comment_lines = []
        for c in comments:
            ts = f" @{c['time_stamp']}s" if c.get("time_stamp") is not None else ""
            comment_lines.append(f"[{c['user_name']}{ts}] {c['content']}")
            for r in c.get("children_comments") or []:
                comment_lines.append(f"  └─ [{r['user_name']}] {r['content']}")
        _save(save_dir, vid, "comments.txt", "\n".join(comment_lines))
        parts.append("## Comments\n")
        for c in comments:
            ts = f" @{c['time_stamp']}s" if c.get("time_stamp") is not None else ""
            parts.append(f"- [{c['user_name']}{ts}] {c['content']}")
            for r in c.get("children_comments") or []:
                parts.append(f"  - [{r['user_name']}] {r['content']}")

    combined = "\n".join(parts)
    path = _save(save_dir, vid, "details.md", combined)
    return _with_saved(combined, path)


@mcp.tool(
    tags={"read"},
    timeout=30.0,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def get_user(
    ctx: Context,
    user_id: Annotated[str, "The Loom user ID"],
) -> str:
    """Get a Loom user's profile by their ID — name, email, company, and avatar."""
    client = _get_client(ctx)
    user = await _call(client.get_user_by_id(_id(user_id, "user ID")))
    if not user:
        raise ToolError(f"User not found: {user_id}")
    return json.dumps(user, indent=2)


@mcp.tool(
    tags={"read"},
    timeout=30.0,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def search_workspace_tags(
    ctx: Context,
    query: Annotated[str, "Search query for tags"],
) -> str:
    """Search for tags in your Loom workspace."""
    client = _get_client(ctx)
    tags = await _call(client.search_workspace_tags(query))
    if not tags:
        return f"No tags matching '{query}'"
    return json.dumps(tags, indent=2)


@mcp.tool(
    tags={"read"},
    timeout=30.0,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def get_folder(
    ctx: Context,
    folder_id: Annotated[str, "The Loom folder ID"],
) -> str:
    """Get details of a Loom folder including name, visibility, and creator."""
    client = _get_client(ctx)
    folder = await _call(client.get_folder(_id(folder_id, "folder ID")))
    if not folder:
        raise ToolError(f"Folder not found: {folder_id}")
    return json.dumps(folder, indent=2)


# ---------------------------------------------------------------------------
# Write Tools
# ---------------------------------------------------------------------------


@mcp.tool(
    tags={"write"},
    timeout=30.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
    },
)
async def update_video_name(
    ctx: Context,
    video_id: Annotated[str, "The Loom video ID"],
    name: Annotated[str, "The new video name"],
) -> str:
    """Rename a Loom video. Overwrites the existing name."""
    client = _get_client(ctx)
    result = await _call(client.update_video_name(_id(video_id, "video ID"), name))
    return f"Renamed to: {result.get('name', name)}"


@mcp.tool(
    tags={"write"},
    timeout=30.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
    },
)
async def update_video_description(
    ctx: Context,
    video_id: Annotated[str, "The Loom video ID"],
    description: Annotated[str, "The new video description"],
) -> str:
    """Update the description of a Loom video. Overwrites the existing description."""
    client = _get_client(ctx)
    result = await _call(
        client.update_video_description(_id(video_id, "video ID"), description)
    )
    return json.dumps(result, indent=2)


@mcp.tool(
    tags={"write"},
    timeout=30.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
    },
)
async def update_video_settings(
    ctx: Context,
    video_id: Annotated[str, "The Loom video ID"],
    settings: Annotated[
        dict,
        'Settings to update (e.g. {"download_enabled": true, "comments_enabled": false})',
    ],
) -> str:
    """Update settings on a Loom video such as download_enabled, comments_enabled, etc."""
    client = _get_client(ctx)
    result = await _call(
        client.update_video_settings(_id(video_id, "video ID"), settings)
    )
    return json.dumps(result, indent=2)


@mcp.tool(
    tags={"write"},
    timeout=30.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
    },
)
async def create_comment(
    ctx: Context,
    video_id: Annotated[str, "The Loom video ID"],
    content: Annotated[str, "The comment text"],
    timestamp: Annotated[
        int,
        Field(
            description="Timestamp in seconds to attach comment to (default 0)", ge=0
        ),
    ] = 0,
) -> str:
    """Post a comment on a Loom video. Each call creates a new comment."""
    client = _get_client(ctx)
    result = await _call(
        client.create_comment(_id(video_id, "video ID"), content, timestamp)
    )
    return json.dumps(result, indent=2)


@mcp.tool(
    tags={"write"},
    timeout=30.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
    },
)
async def edit_comment(
    ctx: Context,
    comment_id: Annotated[str, "The comment ID"],
    video_id: Annotated[str, "The Loom video ID the comment belongs to"],
    content: Annotated[str, "The new comment text"],
) -> str:
    """Edit an existing comment on a Loom video. Overwrites the comment text."""
    client = _get_client(ctx)
    result = await _call(
        client.edit_comment(
            _id(comment_id, "comment ID"), _id(video_id, "video ID"), content
        )
    )
    return json.dumps(result, indent=2)


@mcp.tool(
    tags={"write"},
    timeout=30.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
    },
)
async def delete_comment(
    ctx: Context,
    comment_id: Annotated[str, "The comment ID"],
) -> str:
    """Delete a comment from a Loom video. This cannot be undone."""
    client = _get_client(ctx)
    result = await _call(client.delete_comment(_id(comment_id, "comment ID")))
    return f"Comment deleted: {result}"


@mcp.tool(
    tags={"write"},
    timeout=30.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
    },
)
async def create_task(
    ctx: Context,
    video_id: Annotated[str, "The Loom video ID"],
    content: Annotated[str, "The task/action item text"],
    timestamp: Annotated[
        int,
        Field(description="Timestamp in seconds to attach task to (default 0)", ge=0),
    ] = 0,
) -> str:
    """Create an action item (task) on a Loom video. Each call creates a new task."""
    client = _get_client(ctx)
    result = await _call(
        client.create_task(_id(video_id, "video ID"), content, timestamp)
    )
    return json.dumps(result, indent=2)


@mcp.tool(
    tags={"write"},
    timeout=30.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
    },
)
async def update_task(
    ctx: Context,
    task_id: Annotated[str, "The task ID"],
    content: Annotated[str, "The new task content"],
) -> str:
    """Update the content of an action item (task) on a Loom video."""
    client = _get_client(ctx)
    result = await _call(client.update_video_task(_id(task_id, "task ID"), content))
    return json.dumps(result, indent=2)


@mcp.tool(
    tags={"write"},
    timeout=30.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
    },
)
async def delete_task(
    ctx: Context,
    task_id: Annotated[str, "The task ID"],
) -> str:
    """Delete an action item (task) from a Loom video. This cannot be undone."""
    client = _get_client(ctx)
    result = await _call(client.delete_task(_id(task_id, "task ID")))
    return json.dumps(result, indent=2)


@mcp.tool(
    tags={"write"},
    timeout=30.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def approve_task(
    ctx: Context,
    task_id: Annotated[str, "The task ID"],
) -> str:
    """Mark an action item (task) as approved on a Loom video."""
    client = _get_client(ctx)
    result = await _call(client.approve_task(_id(task_id, "task ID")))
    return json.dumps(result, indent=2)


@mcp.tool(
    tags={"write"},
    timeout=30.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def respond_to_task(
    ctx: Context,
    task_id: Annotated[str, "The task ID"],
    responded: Annotated[bool, "True to mark as responded, False to unmark"] = True,
) -> str:
    """Respond to an action item (task) on a Loom video."""
    client = _get_client(ctx)
    result = await _call(client.respond_to_task(_id(task_id, "task ID"), responded))
    return json.dumps(result, indent=2)


@mcp.tool(
    tags={"write"},
    timeout=30.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
    },
)
async def add_reaction(
    ctx: Context,
    video_id: Annotated[str, "The Loom video ID"],
    time: Annotated[
        int, Field(description="Timestamp in seconds for the reaction", ge=0)
    ],
    reaction_type: Annotated[
        str, "The reaction type — use get_frequent_reactions to see valid values"
    ],
) -> str:
    """Add an emoji reaction to a Loom video at a specific timestamp.

    Use get_frequent_reactions to discover valid reaction type values.
    """
    client = _get_client(ctx)
    result = await _call(
        client.add_reaction(_id(video_id, "video ID"), time, reaction_type)
    )
    if result.get("message"):
        raise ToolError(f"Failed to add reaction: {result['message']}")
    return json.dumps(result, indent=2)


@mcp.tool(
    tags={"write"},
    timeout=30.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
    },
)
async def delete_reaction(
    ctx: Context,
    reaction_id: Annotated[str, "The reaction ID"],
) -> str:
    """Delete an emoji reaction from a Loom video."""
    client = _get_client(ctx)
    result = await _call(client.delete_reaction(_id(reaction_id, "reaction ID")))
    return f"Reaction deleted: {result}"


@mcp.tool(
    tags={"write"},
    timeout=30.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def toggle_following(
    ctx: Context,
    video_id: Annotated[str, "The Loom video ID"],
    follow: Annotated[bool, "True to follow, False to unfollow"],
) -> str:
    """Follow or unfollow a Loom video to get notifications."""
    client = _get_client(ctx)
    result = await _call(client.toggle_following(_id(video_id, "video ID"), follow))
    return json.dumps(result, indent=2)


@mcp.tool(
    tags={"write"},
    timeout=30.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
    },
)
async def delete_video(
    ctx: Context,
    video_id: Annotated[str, "The Loom video ID"],
) -> str:
    """Permanently delete a Loom video. This cannot be undone.

    To temporarily remove a video, use archive_videos instead. To recover a recently deleted video, use recover_video.
    """
    client = _get_client(ctx)
    result = await _call(client.delete_video(_id(video_id, "video ID")))
    return f"Video deleted: {result}"


@mcp.tool(
    tags={"write"},
    timeout=30.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
    },
)
async def archive_videos(
    ctx: Context,
    video_ids: Annotated[list[str], "List of Loom video IDs to archive"],
    archive: Annotated[bool, "True to archive, False to unarchive"] = True,
) -> str:
    """Archive or unarchive Loom videos. Archived videos are hidden but not deleted."""
    client = _get_client(ctx)
    result = await _call(client.archive_videos(_ids(video_ids, "video ID"), archive))
    return json.dumps(result, indent=2)


@mcp.tool(
    tags={"write"},
    timeout=30.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
    },
)
async def duplicate_video(
    ctx: Context,
    video_id: Annotated[str, "The Loom video ID"],
) -> str:
    """Duplicate a Loom video. Creates a new copy each time."""
    client = _get_client(ctx)
    result = await _call(client.duplicate_video(_id(video_id, "video ID")))
    return json.dumps(result, indent=2)


@mcp.tool(
    tags={"write"},
    timeout=30.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def add_to_watch_later(
    ctx: Context,
    video_id: Annotated[str, "The Loom video ID"],
    minutes_from_utc: Annotated[
        int, "Timezone offset in minutes from UTC (default 0)"
    ] = 0,
) -> str:
    """Add a Loom video to your Watch Later list."""
    client = _get_client(ctx)
    result = await _call(
        client.add_to_watch_later(_id(video_id, "video ID"), minutes_from_utc)
    )
    return json.dumps(result, indent=2)


@mcp.tool(
    tags={"write"},
    timeout=30.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
    },
)
async def remove_from_watch_later(
    ctx: Context,
    video_id: Annotated[str, "The Loom video ID"],
) -> str:
    """Remove a Loom video from your Watch Later list."""
    client = _get_client(ctx)
    result = await _call(client.remove_from_watch_later(_id(video_id, "video ID")))
    return json.dumps(result, indent=2)


@mcp.tool(
    tags={"write"},
    timeout=30.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
    },
)
async def create_folder(
    ctx: Context,
    name: Annotated[str, "The folder name"],
) -> str:
    """Create a new Loom folder. Each call creates a new folder."""
    client = _get_client(ctx)
    result = await _call(client.create_folder(name))
    return json.dumps(result, indent=2)


@mcp.tool(
    tags={"write"},
    timeout=30.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
    },
)
async def rename_folder(
    ctx: Context,
    folder_id: Annotated[str, "The Loom folder ID"],
    name: Annotated[str, "The new folder name"],
) -> str:
    """Rename a Loom folder."""
    client = _get_client(ctx)
    result = await _call(client.rename_folder(_id(folder_id, "folder ID"), name))
    return json.dumps(result, indent=2)


@mcp.tool(
    tags={"write"},
    timeout=30.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
    },
)
async def delete_folders(
    ctx: Context,
    folder_ids: Annotated[list[str], "List of Loom folder IDs to delete"],
) -> str:
    """Delete one or more Loom folders. This cannot be undone."""
    client = _get_client(ctx)
    result = await _call(client.delete_folders(_ids(folder_ids, "folder ID")))
    return json.dumps(result, indent=2)


@mcp.tool(
    tags={"write"},
    timeout=30.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
    },
)
async def move_videos(
    ctx: Context,
    video_ids: Annotated[list[str], "List of Loom video IDs to move"],
    folder_id: Annotated[str, "The destination folder ID"],
) -> str:
    """Move one or more Loom videos to a different folder."""
    client = _get_client(ctx)
    result = await _call(
        client.bulk_move_videos(
            _ids(video_ids, "video ID"), _id(folder_id, "folder ID")
        )
    )
    return json.dumps(result, indent=2)


@mcp.tool(
    tags={"write"},
    timeout=30.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
    },
)
async def move_folders(
    ctx: Context,
    folder_ids: Annotated[list[str], "List of Loom folder IDs to move"],
    destination_folder_id: Annotated[str, "The destination parent folder ID"],
) -> str:
    """Move one or more Loom folders into a different parent folder."""
    client = _get_client(ctx)
    result = await _call(
        client.bulk_move_folders(
            _ids(folder_ids, "folder ID"), _id(destination_folder_id, "folder ID")
        )
    )
    return json.dumps(result, indent=2)


@mcp.tool(
    tags={"write"},
    timeout=30.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def recover_video(
    ctx: Context,
    video_id: Annotated[str, "The Loom video ID"],
) -> str:
    """Recover a deleted Loom video from the trash."""
    client = _get_client(ctx)
    result = await _call(client.recover_video(_id(video_id, "video ID")))
    return json.dumps(result, indent=2)


@mcp.tool(
    tags={"write"},
    timeout=30.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def pin_video(
    ctx: Context,
    video_id: Annotated[str, "The Loom video ID"],
    pinned: Annotated[bool, "True to pin, False to unpin"] = True,
) -> str:
    """Pin or unpin a Loom video in your library."""
    client = _get_client(ctx)
    await _call(client.update_video_pin_status(_id(video_id, "video ID"), pinned))
    action = "Pinned" if pinned else "Unpinned"
    return f"{action} video {video_id}"


@mcp.tool(
    tags={"write"},
    timeout=30.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def add_comment_reaction(
    ctx: Context,
    comment_id: Annotated[str, "The comment GUID"],
    reaction: Annotated[str, "The reaction emoji string (e.g. 'heart', '+1', 'fire')"],
    comment_type: Annotated[str, "Comment type: COMMENT or REPLY"] = "COMMENT",
) -> str:
    """Add an emoji reaction to a comment on a Loom video."""
    client = _get_client(ctx)
    result = await _call(
        client.add_comment_reaction(
            _id(comment_id, "comment ID"), reaction, comment_type
        )
    )
    return json.dumps(result, indent=2)


@mcp.tool(
    tags={"write"},
    timeout=30.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def toggle_following_tag(
    ctx: Context,
    tag: Annotated[str, "The tag name to follow/unfollow"],
    follow: Annotated[bool, "True to follow, False to unfollow"],
) -> str:
    """Follow or unfollow a tag in your Loom workspace to get notifications."""
    client = _get_client(ctx)
    await _call(client.toggle_following_tag(tag, follow))
    action = "Following" if follow else "Unfollowed"
    return f"{action} tag '{tag}'"


@mcp.tool(
    tags={"write"},
    timeout=30.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def share_videos_to_spaces(
    ctx: Context,
    video_ids: Annotated[list[str], "List of Loom video IDs to share"],
    space_ids: Annotated[list[str], "List of Loom space IDs to share to"],
) -> str:
    """Share one or more Loom videos to one or more spaces."""
    client = _get_client(ctx)
    result = await _call(
        client.batch_share_videos_to_spaces(
            _ids(video_ids, "video ID"), _ids(space_ids, "space ID")
        )
    )
    return json.dumps(result, indent=2)


@mcp.tool(
    tags={"write"},
    timeout=30.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
    },
)
async def regenerate_mp4(
    ctx: Context,
    video_id: Annotated[str, "The Loom video ID"],
) -> str:
    """Trigger MP4 regeneration for a Loom video. Use this when get_download_url reports that no URL is available. Regeneration is asynchronous — after triggering, wait ~30 seconds then retry get_download_url."""
    client = _get_client(ctx)
    result = await _call(client.regenerate_mp4(_id(video_id, "video ID")))
    if result.get("message"):
        raise ToolError(f"Failed to regenerate MP4: {result['message']}")
    return json.dumps(result, indent=2)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
