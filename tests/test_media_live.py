"""Live integration tests for download_media.

Skipped unless LOOM_COOKIE and LOOM_TEST_VIDEO_ID are set and ffmpeg is on PATH.
LOOM_TEST_VIDEO_ID should be a video you can view that is at least 60 seconds
long and has more than one video bitrate. Run with:

    LOOM_COOKIE="connect.sid=..." LOOM_TEST_VIDEO_ID=... \
        uv run --with pytest python -m pytest tests/test_media_live.py -v
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from loom_mcp import media
from loom_mcp.client import LoomClient

VIDEO_ID = os.environ.get("LOOM_TEST_VIDEO_ID", "")

pytestmark = pytest.mark.skipif(
    not os.environ.get("LOOM_COOKIE") or not VIDEO_ID or not shutil.which("ffmpeg"),
    reason="needs LOOM_COOKIE, LOOM_TEST_VIDEO_ID and ffmpeg",
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


@pytest.fixture
async def manifest(loom):
    """Parsed representations for the test video, used to derive expectations."""
    url = await loom.get_cdn_url(VIDEO_ID, "DASH")
    assert url and "playlistmultibitrate.mpd" in url
    assert "Signature=" in url
    xml = await media._fetch(loom._http, url)
    reps = media.parse_mpd(xml.decode("utf-8", "replace"))
    assert reps
    return reps


@pytest.mark.anyio
async def test_get_cdn_url_returns_dash_manifest(manifest):
    assert media.pick_representation(manifest, "audio") is not None
    assert media.pick_representation(manifest, "video") is not None


@pytest.mark.anyio
async def test_audio_full(loom, manifest, tmp_path):
    expected = media.duration_seconds(media.pick_representation(manifest, "audio"))
    out = await loom.download_media(VIDEO_ID, tmp_path / "full.opus", kind="audio")
    info = _probe(out)
    assert abs(float(info["format"]["duration"]) - expected) < 2.0
    assert info["streams"][0]["codec_type"] == "audio"
    assert info["streams"][0]["codec_name"] == "opus"


@pytest.mark.anyio
async def test_audio_trim(loom, tmp_path):
    out = await loom.download_media(
        VIDEO_ID, tmp_path / "clip.m4a", kind="audio", start=15, end=45
    )
    info = _probe(out)
    assert abs(float(info["format"]["duration"]) - 30.0) < 1.0


@pytest.mark.anyio
async def test_video_trim_small(loom, manifest, tmp_path):
    small = media.pick_representation(manifest, "video", "small")
    best = media.pick_representation(manifest, "video", "best")
    assert small["bandwidth"] <= best["bandwidth"]
    out = await loom.download_media(
        VIDEO_ID,
        tmp_path / "clip.mp4",
        kind="video",
        quality="small",
        start=15,
        end=35,
    )
    info = _probe(out)
    assert abs(float(info["format"]["duration"]) - 20.0) < 1.0
    kinds = {s["codec_type"]: s for s in info["streams"]}
    assert {"video", "audio"} <= set(kinds)
    assert kinds["video"]["width"] == int(small["width"])
    assert kinds["video"]["height"] == int(small["height"])
