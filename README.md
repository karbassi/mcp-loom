# Loom MCP Server

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

[MCP](https://modelcontextprotocol.io) server exposing 59 tools for Loom's internal GraphQL API. Works with Claude, Cursor, or any MCP-compatible client.

## Features

- List, search, and get detailed metadata for Loom videos
- Read transcripts, captions, AI summaries, chapters, and key takeaways
- Save any fetched content to disk on demand via `save_dir` parameter
- Manage comments, tasks, reactions, and tags
- Organize with folders, spaces, and watch lists
- Update video settings, share to spaces, and more

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager

## Setup

### Authentication

> [!IMPORTANT]
> This server uses Loom's internal GraphQL API via a browser session cookie. There is no official API key — you must grab the cookie from your browser.

1. Open [loom.com](https://www.loom.com) in your browser
2. Open DevTools (F12) → Application → Cookies → `https://www.loom.com`
3. Copy the **value** of the `connect.sid` cookie (starts with `s%3A...`)
4. Paste it as `LOOM_COOKIE` in your MCP client config below, prefixed with `connect.sid=`

The cookie lasts about 30 days. See [Troubleshooting](#troubleshooting) if you get auth errors.

### Installation

Pick your MCP client below for the appropriate config. Each uses `uvx` to install and run directly from GitHub — no clone needed.

<details>
<summary><b>Claude Desktop</b></summary>

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "loom": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "git+https://github.com/karbassi/loom-mcp.git", "loom-mcp"],
      "env": {
        "LOOM_COOKIE": "connect.sid=s%3A..."
      }
    }
  }
}
```

</details>

<details>
<summary><b>Claude Code</b></summary>

```sh
claude mcp add loom -- uvx --from git+https://github.com/karbassi/loom-mcp.git loom-mcp
```

Then set the env var in your shell or `.env`:

```sh
export LOOM_COOKIE="connect.sid=s%3A..."
```

</details>

<details>
<summary><b>Cursor</b></summary>

Add to your Cursor MCP settings (`.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "loom": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "git+https://github.com/karbassi/loom-mcp.git", "loom-mcp"],
      "env": {
        "LOOM_COOKIE": "connect.sid=s%3A..."
      }
    }
  }
}
```

</details>

<details>
<summary><b>VS Code</b></summary>

Add to your VS Code settings (`.vscode/mcp.json`):

```json
{
  "mcpServers": {
    "loom": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "git+https://github.com/karbassi/loom-mcp.git", "loom-mcp"],
      "env": {
        "LOOM_COOKIE": "connect.sid=s%3A..."
      }
    }
  }
}
```

</details>

<details>
<summary><b>Local clone</b></summary>

```sh
git clone git@github.com:karbassi/loom-mcp.git
cd loom-mcp
uv sync
uv run loom-mcp
```

Or reference it from any MCP client:

```json
{
  "mcpServers": {
    "loom": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", "/path/to/loom-mcp", "loom-mcp"],
      "env": {
        "LOOM_COOKIE": "connect.sid=s%3A..."
      }
    }
  }
}
```

</details>

## Tools

### Read (31 tools)

Tools marked with **Save** accept an optional `save_dir` parameter — see [Saving to disk](#saving-to-disk).

| Tool | Description | Save |
|---|---|:---:|
| `list_videos` | List your videos, sorted by most recent | |
| `search_videos` | AI-powered semantic search | |
| `get_video` | Video metadata (name, duration, owner, views) | `metadata.json` |
| `get_transcript` | Full transcript with timestamps and speakers | `transcript.txt` |
| `get_captions` | WebVTT captions with start+end timestamps per cue | `captions.vtt` |
| `get_summary` | AI-generated summary | `summary.txt` |
| `get_chapters` | AI-generated chapters | `chapters.txt` |
| `get_description` | AI-generated detailed description with timestamped sections | `description.txt` |
| `get_key_takeaways` | AI-generated key takeaways | `takeaways.txt` |
| `get_comments` | Comments and replies | `comments.txt` |
| `get_tasks` | AI-generated action items | `tasks.txt` |
| `get_reactions` | Emoji reactions | `reactions.txt` |
| `get_tags` | Video tags | `tags.txt` |
| `get_backlinks` | External references (where the video is shared/embedded) | `backlinks.txt` |
| `get_meeting_notes` | Confluence meeting notes URL | |
| `get_confluence_pages` | Linked Confluence pages | |
| `get_download_url` | Signed MP4 download URL | |
| `download_media` | Download audio or video to a local file (works when MP4 export is disabled; optional `start`/`end` trim; needs `ffmpeg`) | `out_path` |
| `get_video_details` | All-in-one: metadata + transcript + chapters + summary + comments + tasks | `details.md` + all above |
| `list_folders` | List your folders | |
| `list_spaces` | List your workspaces | |
| `get_space` | Space details (name, privacy, primary) | |
| `search_folders` | Search folders by name | |
| `get_folder` | Folder details | |
| `get_last_watch_time` | Last timestamp where you stopped watching | |
| `get_watch_later_count` | Number of videos in your Watch Later list | |
| `get_total_videos_count` | Total videos created by a user | |
| `get_frequent_reactions` | Your most-used emoji reaction types | |
| `get_comment_reactions` | Emoji reactions on a specific comment | |
| `get_user` | User profile by ID (name, email, company, avatar) | |
| `search_workspace_tags` | Search tags in your workspace | |

### Write (29 tools)

> [!NOTE]
> Write tools modify your Loom data. Destructive actions (delete, archive, move) cannot always be undone.

| Tool | Description |
|---|---|
| `update_video_name` | Rename a video |
| `update_video_description` | Update video description |
| `update_video_settings` | Update video settings (downloads, comments, etc.) |
| `create_comment` | Post a comment (with optional timestamp) |
| `edit_comment` | Edit an existing comment |
| `delete_comment` | Delete a comment |
| `create_task` | Create an action item on a video |
| `update_task` | Update the content of an action item |
| `delete_task` | Delete an action item |
| `approve_task` | Mark a task as approved |
| `respond_to_task` | Respond to a task |
| `add_reaction` | Add an emoji reaction at a timestamp |
| `delete_reaction` | Delete a reaction |
| `add_comment_reaction` | React to a comment with an emoji |
| `toggle_following` | Follow/unfollow a video |
| `toggle_following_tag` | Follow/unfollow a workspace tag |
| `archive_videos` | Archive or unarchive videos |
| `duplicate_video` | Duplicate a video |
| `delete_video` | Permanently delete a video |
| `recover_video` | Recover a deleted video from trash |
| `pin_video` | Pin or unpin a video in your library |
| `add_to_watch_later` | Add to Watch Later list |
| `remove_from_watch_later` | Remove from Watch Later list |
| `create_folder` | Create a new folder |
| `rename_folder` | Rename a folder |
| `delete_folders` | Delete folders |
| `move_videos` | Move videos to a different folder |
| `move_folders` | Move folders into a different parent folder |
| `share_videos_to_spaces` | Share videos to one or more spaces |

## Saving to disk

Most per-video read tools accept an optional `save_dir` parameter. When provided, the tool saves its output to `{save_dir}/{video_id}/` and returns the file path alongside the content. When omitted, nothing is saved.

Just ask naturally — *"get the details from my last meeting and save them to the `ask` directory"* — and the LLM will pass `save_dir="ask"` to the tool.

`get_video_details` saves each piece individually (`metadata.json`, `transcript.txt`, `summary.txt`, etc.) plus a combined `details.md`.

## Troubleshooting

### Auth errors

If you get auth errors, your `connect.sid` cookie has expired (~30 days). Grab a fresh one from your browser using the steps in [Authentication](#authentication).

### Debugging with MCP Inspector

```sh
npx @modelcontextprotocol/inspector uvx --from git+https://github.com/karbassi/loom-mcp.git loom-mcp
```

## Contributing

Issues and pull requests are welcome on [GitHub](https://github.com/karbassi/loom-mcp).

## License

[MIT](LICENSE)
