# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added

- `download_media` tool: download a video's audio or video to a local file via Loom's signed DASH manifest, including notetaker recordings where MP4 export is disabled. Supports `start`/`end` trimming (only the overlapping segments are fetched) and `quality` selection for video. Requires `ffmpeg` on PATH.
- `LoomClient.get_cdn_url` for the raw signed CDN URL (`DASH`, `M3U8`, or `MP4`)
- GitHub Actions CI workflow (lint, format, test) triggered on push and PRs
- PyPI publish workflow using OIDC trusted publishing, triggered on version tags
- mise tasks: `install`, `lint`, `format`, `test`, `build`
- `install-hooks` mise task and `mise enter` hook for automatic pre-commit setup

### Changed

- Renamed PyPI package from `loom-mcp` to `mcp-loom`
- Pre-commit hook now auto-formats and re-stages files

### Fixed

- Publish workflow permissions include `contents: read` so `actions/checkout` works alongside `id-token: write`

## [1.1.0] - 2026-02-17

### Added

- Optional `save_dir` parameter on all per-video read tools (`get_video`, `get_transcript`, `get_captions`, `get_summary`, `get_chapters`, `get_description`, `get_key_takeaways`, `get_comments`, `get_tasks`, `get_reactions`, `get_tags`, `get_backlinks`, `get_video_details`)
- When `save_dir` is provided, tool output is saved to `{save_dir}/{video_id}/` with appropriate filenames (e.g. `transcript.txt`, `captions.vtt`, `metadata.json`)
- `get_video_details` saves each piece individually plus a combined `details.md`
- Saved file path is returned alongside the content

## [1.0.2] - 2026-02-10

### Added

- `get_space` tool — get details of a space by ID (parallels `get_folder`)
- `limit` parameter on `search_videos` (default 50, max 200)

### Changed

- `search_videos` now uses the paginated `SearchVideos` endpoint instead of the semantic `Search` endpoint — faster and no longer capped at 10 results
- Remove unused `fetch_videos_by_id` and `get_all_videos` from client

## [1.0.1] - 2026-02-10

### Fixed

- 401 error message now says to refresh the browser cookie instead of referencing removed `login.js`
- `.env` and `auth.json` path resolution no longer breaks when installed via `uvx` (gracefully skipped when no local `pyproject.toml` is found)
- Clear error message when neither `LOOM_COOKIE` nor `LOOM_AUTH_FILE` is set

### Changed

- `get_video_details` now fetches transcript, chapters, summary, comments, and tasks concurrently via `asyncio.gather` instead of sequentially
- Rewrite README with per-client install instructions, badges, and improved auth docs
- Add MIT license, CONTRIBUTING.md

## [1.0.0] - 2026-02-10

### Changed

- Restructure to `src/` layout (`src/loom_mcp/server.py`, `src/loom_mcp/client.py`)
- Move tests to `tests/` directory
- Add hatchling build-system for proper packaging
- Resolve `.env`/`auth.json` paths by walking up to `pyproject.toml`
- Lower `requires-python` from 3.14 to 3.11
- Remove `.python-version` (redundant with `requires-python`)
- Simplify README auth docs, drop parent repo references
- Rename package to `loom-mcp` with console script entry point
- Switch to `@lifespan` decorator
- Add `read`/`write` tags and `timeout=30.0` to all tools
- Use `uvx` entry point in `fastmcp.json`

## [0.1.0] - 2026-02-08

### Added

- FastMCP server exposing 58 tools (29 read, 29 write) for Loom's internal GraphQL API
- Async Python client using httpx with concurrency limiter (5 concurrent requests)
- Auth via `LOOM_COOKIE` env var, `LOOM_AUTH_FILE` path, or `auth.json`
- Auto-load `.env` file for auth config
- Tool annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`) on all tools
- Input ID validation via `_id()`/`_ids()` helpers to reject injection-style inputs
- `LoomAPIError` and `ToolError` for actionable error messages (401, 403, connection errors)
- Lifespan-managed httpx client with proper cleanup
- 31 tests covering tool registration, annotations, ID validation, happy paths, and error paths
- `fastmcp.json` for FastMCP run support
- Ruff T20 lint rule to ban `print()` in stdio server code
