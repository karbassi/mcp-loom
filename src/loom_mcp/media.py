"""Download Loom media (audio/video) from the signed DASH manifest.

Loom exposes a CloudFront-signed DASH manifest via
``nullableRawCdnUrl(acceptableMimes: [DASH])``. This works even when MP4 export
is disabled (e.g. notetaker recordings, where ``getVideoTranscodedUrl`` is null).

ffmpeg cannot ingest the signed manifest directly because it does not propagate
the ``Policy/Signature/Key-Pair-Id`` query string to segment requests (403). So we
parse the MPD ourselves, download the init segment plus the needed media segments
with the signed query appended, concatenate each track into a WebM stream, and
then hand the stitched tracks to ffmpeg for mux/transcode/trim.

Segment and manifest fetches need no cookie; the URL signature is the credential.
"""

import asyncio
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from loom_mcp.client import LoomAPIError

_DASH_NS = "urn:mpeg:dash:schema:mpd:2011"
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
_MAX_CONCURRENCY = 24

# Containers that can hold Loom's native Opus/VP9 streams without re-encoding.
COPY_AUDIO_SUFFIXES = {".webm", ".opus", ".ogg", ".mka", ".mkv"}
COPY_VIDEO_SUFFIXES = {".webm", ".mkv"}
QUALITIES = {"best", "small"}


class MediaError(LoomAPIError):
    """Raised when media download or ffmpeg processing fails."""


def _tag(name: str) -> str:
    return f"{{{_DASH_NS}}}{name}"


_ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<d>[\d.]+)D)?"
    r"(?:T(?:(?P<h>[\d.]+)H)?(?:(?P<m>[\d.]+)M)?(?:(?P<s>[\d.]+)S)?)?$"
)


def _parse_iso_duration(value: str | None) -> float | None:
    """Parse an ISO 8601 duration like ``PT1H2M3.5S`` into seconds."""
    if not value:
        return None
    m = _ISO_DURATION_RE.match(value.strip())
    if not m or not any(m.groupdict().values()):
        return None
    parts = {k: float(v) if v else 0.0 for k, v in m.groupdict().items()}
    return parts["d"] * 86400 + parts["h"] * 3600 + parts["m"] * 60 + parts["s"]


def _expand_timeline(
    entries: list[tuple[int | None, int, int]], period_end: int | None
) -> list[tuple[int, int]]:
    """Expand ``(t, d, r)`` SegmentTimeline entries into ``(t, d)`` segments.

    ``r`` is the number of *additional* repeats. A negative ``r`` means repeat
    until the next entry's ``t`` (or the period end for the last entry).
    """
    timeline: list[tuple[int, int]] = []
    t = 0
    for i, (t_attr, d, r) in enumerate(entries):
        if t_attr is not None:
            t = t_attr
        if r < 0:
            if i + 1 < len(entries) and entries[i + 1][0] is not None:
                until = entries[i + 1][0]
            elif period_end is not None:
                until = period_end
            else:
                raise MediaError(
                    "SegmentTimeline uses r=-1 without a following t or period duration"
                )
            count = max(0, (until - t + d - 1) // d)  # type: ignore[operator]
        else:
            count = r + 1
        for _ in range(count):
            timeline.append((t, d))
            t += d
    return timeline


def _manifest_base(url: str) -> tuple[str, str]:
    """Split a signed manifest URL into (directory base URL, signed query string)."""
    p = urlsplit(url)
    base_dir = p.path.rsplit("/", 1)[0] + "/"
    return urlunsplit((p.scheme, p.netloc, base_dir, "", "")), p.query


def parse_mpd(xml_text: str) -> list[dict]:
    """Parse a static MPD into a flat list of representations.

    Each representation dict has: contentType, id, bandwidth, codecs, width,
    height, init, media, startNumber, timescale, and timeline — a list of
    ``(t, d)`` tuples in timescale units with ``r`` repeats expanded.
    """
    root = ET.fromstring(xml_text)
    mpd_duration = _parse_iso_duration(root.get("mediaPresentationDuration"))
    reps: list[dict] = []
    for period in root.iter(_tag("Period")):
        period_duration = _parse_iso_duration(period.get("duration")) or mpd_duration
        for aset in period.iter(_tag("AdaptationSet")):
            ctype = aset.get("contentType")
            for rep in aset.findall(_tag("Representation")):
                st = rep.find(_tag("SegmentTemplate"))
                if st is None:
                    st = aset.find(_tag("SegmentTemplate"))
                if st is None:
                    continue
                timescale = int(st.get("timescale", "1"))
                pto = int(st.get("presentationTimeOffset", "0"))
                # Timeline times are period-relative, offset by presentationTimeOffset.
                period_end: int | None = None
                if period_duration is not None:
                    period_end = pto + int(round(period_duration * timescale))
                timeline: list[tuple[int, int]] = []
                tl = st.find(_tag("SegmentTimeline"))
                if tl is not None:
                    entries = [
                        (
                            int(s.get("t")) if s.get("t") is not None else None,  # type: ignore[arg-type]
                            int(s.get("d")),  # type: ignore[arg-type]
                            int(s.get("r", "0")),
                        )
                        for s in tl.findall(_tag("S"))
                    ]
                    timeline = _expand_timeline(entries, period_end)
                if ctype is None:
                    mime = rep.get("mimeType") or aset.get("mimeType") or ""
                    ctype = mime.split("/", 1)[0] or None
                reps.append(
                    {
                        "contentType": ctype,
                        "id": rep.get("id"),
                        "bandwidth": int(rep.get("bandwidth", "0")),
                        "codecs": rep.get("codecs"),
                        "width": rep.get("width"),
                        "height": rep.get("height"),
                        "init": st.get("initialization"),
                        "media": st.get("media"),
                        "startNumber": int(st.get("startNumber", "0")),
                        "timescale": timescale,
                        "timeline": timeline,
                    }
                )
    return reps


def pick_representation(
    reps: list[dict], content_type: str, quality: str = "best"
) -> dict | None:
    """Choose a representation for ``content_type`` ('audio' or 'video').

    Audio has a single representation. For video, ``quality='best'`` picks the
    highest bandwidth and ``'small'`` the lowest.
    """
    if quality not in QUALITIES:
        raise MediaError(f"quality must be one of {sorted(QUALITIES)}")
    candidates = [r for r in reps if r["contentType"] == content_type]
    if not candidates:
        return None
    if content_type == "audio":
        return candidates[0]
    candidates.sort(key=lambda r: r["bandwidth"])
    return candidates[-1] if quality == "best" else candidates[0]


def select_segments(
    rep: dict, start: float | None, end: float | None
) -> tuple[list[int], float]:
    """Return (segment indices overlapping [start, end], offset of first segment).

    Indices are 0-based positions in the timeline; add ``startNumber`` to get the
    ``$Number$`` value. The offset is the start time (seconds) of the first
    selected segment, used to compute the relative ffmpeg ``-ss``.
    """
    ts, timeline = rep["timescale"], rep["timeline"]
    if not timeline:
        return [], 0.0
    if start is None and end is None:
        return list(range(len(timeline))), timeline[0][0] / ts
    indices: list[int] = []
    offset: float | None = None
    for i, (t, d) in enumerate(timeline):
        s0, s1 = t / ts, (t + d) / ts
        if (end is None or s0 < end) and (start is None or s1 > start):
            indices.append(i)
            if offset is None:
                offset = s0
    return indices, (offset or 0.0)


def duration_seconds(rep: dict) -> float:
    """Total duration of a representation's timeline in seconds."""
    if not rep["timeline"]:
        return 0.0
    t0 = rep["timeline"][0][0]
    t_end, d_end = rep["timeline"][-1]
    return (t_end + d_end - t0) / rep["timescale"]


async def _fetch(http: httpx.AsyncClient, url: str) -> bytes:
    try:
        r = await http.get(
            url, headers={"user-agent": _UA, "referer": "https://www.loom.com/"}
        )
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise MediaError(
            f"CDN returned HTTP {e.response.status_code} for {urlsplit(url).path}"
        ) from None
    except httpx.HTTPError as e:
        raise MediaError(f"CDN request failed: {e}") from None
    return r.content


def _expand(template: str, rep: dict) -> str:
    """Expand non-numeric DASH template identifiers."""
    return template.replace("$RepresentationID$", str(rep["id"])).replace(
        "$Bandwidth$", str(rep["bandwidth"])
    )


async def _download_track(
    http: httpx.AsyncClient,
    base: str,
    query: str,
    rep: dict,
    indices: list[int],
    dest: Path,
    sem: asyncio.Semaphore,
) -> None:
    """Fetch init + selected media segments and concatenate them into ``dest``."""

    async def one(i: int) -> tuple[int, bytes]:
        num = rep["startNumber"] + i
        name = _expand(rep["media"], rep).replace("$Number$", str(num))
        async with sem:
            return i, await _fetch(http, f"{base}{name}?{query}")

    init = await _fetch(http, f"{base}{_expand(rep['init'], rep)}?{query}")
    results = await asyncio.gather(*(one(i) for i in indices))
    results.sort(key=lambda x: x[0])
    with open(dest, "wb") as f:
        f.write(init)
        for _, data in results:
            f.write(data)


async def _run_ffmpeg(args: list[str]) -> None:
    """Run ffmpeg without blocking the event loop."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise MediaError("ffmpeg not found on PATH; install it (brew install ffmpeg)")
    proc = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-nostdin",
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await proc.communicate()
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0:
        msg = stderr.decode("utf-8", "replace").strip()[:500]
        raise MediaError(f"ffmpeg failed: {msg}")


def _trim_args(
    start: float | None, end: float | None, track_offset: float
) -> list[str]:
    """ffmpeg input-side trim flags relative to the first downloaded segment."""
    if start is None and end is None:
        return []
    rel_start = max(0.0, (start or 0.0) - track_offset)
    args = ["-ss", f"{rel_start:.3f}"]
    if end is not None:
        rel_end = max(0.0, end - track_offset)
        args += ["-t", f"{max(0.0, rel_end - rel_start):.3f}"]
    return args


async def download_media(
    http: httpx.AsyncClient,
    manifest_url: str,
    out_path: str | Path,
    kind: str = "audio",
    quality: str = "best",
    start: float | None = None,
    end: float | None = None,
    tmp_root: str | Path | None = None,
) -> Path:
    """Download audio or video from a signed DASH manifest to ``out_path``.

    ``kind`` is 'audio' or 'video'. The output container is inferred from the
    ``out_path`` extension. ``start``/``end`` (seconds) trim the result; only the
    overlapping segments are downloaded. Requires ffmpeg on PATH.
    """
    if kind not in ("audio", "video"):
        raise MediaError("kind must be 'audio' or 'video'")
    if quality not in QUALITIES:
        raise MediaError(f"quality must be one of {sorted(QUALITIES)}")
    if start is not None and start < 0:
        raise MediaError("start must be >= 0")
    if start is not None and end is not None and end <= start:
        raise MediaError("end must be greater than start")
    if not shutil.which("ffmpeg"):
        raise MediaError("ffmpeg not found on PATH; install it (brew install ffmpeg)")

    base, query = _manifest_base(manifest_url)
    manifest = await _fetch(http, manifest_url)
    try:
        reps = parse_mpd(manifest.decode("utf-8", "replace"))
    except ET.ParseError as e:
        raise MediaError(f"Could not parse DASH manifest: {e}") from None
    if not reps:
        raise MediaError("DASH manifest has no representations")

    audio = pick_representation(reps, "audio")
    video = pick_representation(reps, "video", quality) if kind == "video" else None
    if audio is None and (kind == "audio" or video is None):
        raise MediaError("DASH manifest has no audio track")
    if kind == "video" and video is None:
        raise MediaError("DASH manifest has no video track")

    out_path = Path(out_path).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_parent = Path(tmp_root).expanduser().resolve() if tmp_root else out_path.parent
    tmp_parent.mkdir(parents=True, exist_ok=True)
    # Unique per call so concurrent downloads (e.g. foo.mp4 and foo.m4a, or the
    # same path twice) never share or delete each other's intermediates.
    workdir = Path(tempfile.mkdtemp(prefix=".loom-media-", dir=tmp_parent))
    a_tmp = workdir / "audio.webm"
    v_tmp = workdir / "video.webm"

    sem = asyncio.Semaphore(_MAX_CONCURRENCY)
    tasks = []
    a_off = 0.0
    if audio is not None:
        a_idx, a_off = select_segments(audio, start, end)
        if not a_idx:
            raise MediaError("Requested time range is outside the recording")
        tasks.append(_download_track(http, base, query, audio, a_idx, a_tmp, sem))
    v_off = 0.0
    if video is not None:
        v_idx, v_off = select_segments(video, start, end)
        if not v_idx:
            raise MediaError("Requested time range is outside the recording")
        tasks.append(_download_track(http, base, query, video, v_idx, v_tmp, sem))

    try:
        await asyncio.gather(*tasks)
        suffix = out_path.suffix.lower()
        if kind == "audio":
            # Loom serves Opus; keep the original bits when the container allows.
            codec = ["-c:a", "copy"] if suffix in COPY_AUDIO_SUFFIXES else []
            await _run_ffmpeg(
                [
                    *_trim_args(start, end, a_off),
                    "-i",
                    str(a_tmp),
                    "-vn",
                    *codec,
                    str(out_path),
                ]
            )
        else:
            if suffix in COPY_VIDEO_SUFFIXES:
                codec = ["-c", "copy"]
            else:
                codec = ["-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac"]
            args = [*_trim_args(start, end, v_off), "-i", str(v_tmp)]
            maps = ["-map", "0:v:0"]
            if audio is not None:
                args += [*_trim_args(start, end, a_off), "-i", str(a_tmp)]
                maps += ["-map", "1:a:0"]
            await _run_ffmpeg([*args, *maps, *codec, "-shortest", str(out_path)])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return out_path
