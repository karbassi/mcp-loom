"""Live integration tests for download_media.

Skipped unless LOOM_COOKIE is set (and ffmpeg is on PATH). Run with:

    LOOM_COOKIE="connect.sid=..." uv run --with pytest python -m pytest tests/test_media_live.py -v
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from loom_mcp.client import LoomClient

VIDEO_ID = os.environ.get("LOOM_TEST_VIDEO_ID", "LOOM_TEST_VIDEO_ID")

pytestmark = pytest.mark.skipif(
    not os.environ.get("LOOM_COOKIE") or not shutil.which("ffmpeg"),
    reason="needs LOOM_COOKIE and ffmpeg",
)


def _probe(path: Path) -> dict:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(out)


@pytest.fixture
async def loom():
    cookie = os.environ["LOOM_COOKIE"]
    if not cookie.startswith("connect.sid="):
        cookie = f"connect.sid={cookie}"
    client = LoomClient(cookies=cookie)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_get_cdn_url_returns_dash_manifest(loom):
    url = await loom.get_cdn_url(VIDEO_ID, "DASH")
    assert url and "playlistmultibitrate.mpd" in url
    assert "Signature=" in url


@pytest.mark.anyio
async def test_audio_full(loom, tmp_path):
    out = await loom.download_media(VIDEO_ID, tmp_path / "full.opus", kind="audio")
    info = _probe(out)
    assert abs(float(info["format"]["duration"]) - 3275.3) < 2.0
    assert info["streams"][0]["codec_type"] == "audio"
    assert info["streams"][0]["codec_name"] == "opus"


@pytest.mark.anyio
async def test_audio_trim(loom, tmp_path):
    out = await loom.download_media(
        VIDEO_ID, tmp_path / "clip.m4a", kind="audio", start=1841, end=2252
    )
    info = _probe(out)
    assert abs(float(info["format"]["duration"]) - 411.0) < 1.0


@pytest.mark.anyio
async def test_video_trim_small(loom, tmp_path):
    out = await loom.download_media(
        VIDEO_ID,
        tmp_path / "clip.mp4",
        kind="video",
        quality="small",
        start=1841,
        end=1871,
    )
    info = _probe(out)
    assert abs(float(info["format"]["duration"]) - 30.0) < 1.0
    kinds = {s["codec_type"]: s for s in info["streams"]}
    assert {"video", "audio"} <= set(kinds)
    assert kinds["video"]["width"] == 1280
    assert kinds["video"]["height"] == 720
