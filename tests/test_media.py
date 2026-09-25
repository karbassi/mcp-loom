"""Unit tests for loom_mcp.media (no network, no ffmpeg)."""

import asyncio
import shutil
import tempfile
from pathlib import Path

import httpx
import pytest

from loom_mcp import media
from loom_mcp.media import (
    MediaError,
    COPY_AUDIO_SUFFIXES,
    COPY_VIDEO_SUFFIXES,
    _expand,
    _expand_timeline,
    _parse_iso_duration,
    _manifest_base,
    _trim_args,
    duration_seconds,
    parse_mpd,
    pick_representation,
    select_segments,
)

MANIFEST_URL = (
    "https://luna.loom.com/id/abc123/rev/7/resource/dash/playlistmultibitrate.mpd"
    "?Policy=eyJ&Signature=sig~x&Key-Pair-Id=KP"
)

MPD = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
     mediaPresentationDuration="PT10S" minBufferTime="PT2S"
     profiles="urn:mpeg:dash:profile:isoff-live:2011">
  <Period id="0" start="PT0S">
    <AdaptationSet contentType="audio" mimeType="audio/webm" segmentAlignment="true">
      <Representation id="audio" bandwidth="128000" codecs="opus">
        <SegmentTemplate timescale="1000000" startNumber="0"
            initialization="abc123-audio-init.webm"
            media="abc123-audio-$Number$.webm">
          <SegmentTimeline>
            <S t="0" d="4000000" r="1"/>
            <S d="2000000"/>
          </SegmentTimeline>
        </SegmentTemplate>
      </Representation>
    </AdaptationSet>
    <AdaptationSet contentType="video" mimeType="video/webm" segmentAlignment="true">
      <Representation id="original" bandwidth="3000000" codecs="vp9" width="1920" height="1080">
        <SegmentTemplate timescale="1000000" startNumber="0"
            initialization="abc123-video-init.webm"
            media="abc123-video-$Number$.webm">
          <SegmentTimeline>
            <S t="0" d="2000000" r="4"/>
          </SegmentTimeline>
        </SegmentTemplate>
      </Representation>
      <Representation id="1500000" bandwidth="1500000" codecs="vp9" width="1280" height="720">
        <SegmentTemplate timescale="1000000" startNumber="0"
            initialization="abc123-video-bitrate1500-init.webm"
            media="abc123-video-bitrate1500-$Number$.webm">
          <SegmentTimeline>
            <S t="0" d="2000000" r="4"/>
          </SegmentTimeline>
        </SegmentTemplate>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""


def test_manifest_base_splits_dir_and_query():
    base, query = _manifest_base(MANIFEST_URL)
    assert base == "https://luna.loom.com/id/abc123/rev/7/resource/dash/"
    assert query == "Policy=eyJ&Signature=sig~x&Key-Pair-Id=KP"


def test_parse_mpd_representations():
    reps = parse_mpd(MPD)
    assert [r["id"] for r in reps] == ["audio", "original", "1500000"]
    audio = reps[0]
    assert audio["contentType"] == "audio"
    assert audio["codecs"] == "opus"
    assert audio["init"] == "abc123-audio-init.webm"
    assert audio["media"] == "abc123-audio-$Number$.webm"
    assert audio["startNumber"] == 0
    assert audio["timescale"] == 1_000_000


def test_parse_mpd_expands_repeats():
    reps = parse_mpd(MPD)
    audio = reps[0]
    # S r="1" d=4s -> two 4s segments, then one 2s segment
    assert audio["timeline"] == [
        (0, 4_000_000),
        (4_000_000, 4_000_000),
        (8_000_000, 2_000_000),
    ]
    video = reps[1]
    assert len(video["timeline"]) == 5
    assert video["timeline"][-1] == (8_000_000, 2_000_000)
    assert duration_seconds(audio) == 10.0
    assert duration_seconds(video) == 10.0


def test_pick_representation():
    reps = parse_mpd(MPD)
    assert pick_representation(reps, "audio")["id"] == "audio"
    assert pick_representation(reps, "video", "best")["id"] == "original"
    assert pick_representation(reps, "video", "small")["id"] == "1500000"
    assert pick_representation(reps, "subtitle") is None


def test_select_segments_full_range():
    audio = parse_mpd(MPD)[0]
    idx, off = select_segments(audio, None, None)
    assert idx == [0, 1, 2]
    assert off == 0.0


def test_select_segments_window():
    video = parse_mpd(MPD)[1]  # five 2s segments
    idx, off = select_segments(video, 3.0, 7.0)
    # 3.0 falls in seg 1 [2,4); 7.0 falls in seg 3 [6,8)
    assert idx == [1, 2, 3]
    assert off == 2.0


def test_select_segments_start_only_and_end_only():
    video = parse_mpd(MPD)[1]
    assert select_segments(video, 8.5, None) == ([4], 8.0)
    assert select_segments(video, None, 1.0) == ([0], 0.0)


def test_select_segments_boundary_is_exclusive():
    video = parse_mpd(MPD)[1]
    # end exactly on a segment boundary should not pull in the next segment
    assert select_segments(video, 2.0, 4.0) == ([1], 2.0)


def test_select_segments_out_of_range():
    video = parse_mpd(MPD)[1]
    assert select_segments(video, 50.0, 60.0) == ([], 0.0)


def test_trim_args_relative_to_first_segment():
    assert _trim_args(None, None, 0.0) == []
    assert _trim_args(3.0, 7.0, 2.0) == ["-ss", "1.000", "-t", "4.000"]
    assert _trim_args(3.0, None, 2.0) == ["-ss", "1.000"]
    assert _trim_args(None, 7.0, 0.0) == ["-ss", "0.000", "-t", "7.000"]


class _FakeHTTP:
    """Minimal stand-in for httpx.AsyncClient that serves segments from a dict."""

    def __init__(self, files: dict[str, bytes], query: str):
        self.files = files
        self.query = query
        self.requested: list[str] = []

    async def get(self, url, headers=None):
        self.requested.append(url)
        path, _, q = url.partition("?")
        assert q == self.query, f"signed query not propagated for {url}"
        name = path.rsplit("/", 1)[1]
        if name not in self.files:
            req = httpx.Request("GET", url)
            resp = httpx.Response(403, request=req)
            return resp
        req = httpx.Request("GET", url)
        return httpx.Response(200, content=self.files[name], request=req)


def test_download_track_stitches_init_and_segments_in_order(tmp_path: Path):
    base, query = _manifest_base(MANIFEST_URL)
    video = parse_mpd(MPD)[1]
    files = {"abc123-video-init.webm": b"INIT"}
    for n in range(5):
        files[f"abc123-video-{n}.webm"] = f"S{n}".encode()
    http = _FakeHTTP(files, query)
    dest = tmp_path / "v.webm"
    asyncio.run(
        media._download_track(
            http, base, query, video, [1, 2, 3], dest, asyncio.Semaphore(4)
        )
    )
    assert dest.read_bytes() == b"INITS1S2S3"
    assert len(http.requested) == 4


def test_fetch_403_raises_media_error():
    base, query = _manifest_base(MANIFEST_URL)
    http = _FakeHTTP({}, query)
    with pytest.raises(MediaError, match="HTTP 403"):
        asyncio.run(media._fetch(http, f"{base}missing.webm?{query}"))


def test_download_media_validates_args(tmp_path: Path):
    http = _FakeHTTP({}, "")
    with pytest.raises(MediaError, match="kind"):
        asyncio.run(
            media.download_media(http, MANIFEST_URL, tmp_path / "x.wav", kind="gif")
        )
    with pytest.raises(MediaError, match="end must be greater"):
        asyncio.run(
            media.download_media(http, MANIFEST_URL, tmp_path / "x.wav", start=5, end=5)
        )


def test_expand_template_identifiers():
    rep = {"id": "1500000", "bandwidth": 1500000}
    assert _expand("init-$RepresentationID$.webm", rep) == "init-1500000.webm"
    assert (
        _expand("chunk-$Bandwidth$-$Number$.webm", rep) == "chunk-1500000-$Number$.webm"
    )
    assert _expand("abc-audio-$Number$.webm", rep) == "abc-audio-$Number$.webm"


def test_copy_suffixes_are_opus_vp9_capable():
    assert {".opus", ".webm"} <= COPY_AUDIO_SUFFIXES
    assert ".wav" not in COPY_AUDIO_SUFFIXES and ".m4a" not in COPY_AUDIO_SUFFIXES
    assert COPY_VIDEO_SUFFIXES == {".webm", ".mkv"}


def test_parse_iso_duration():
    assert _parse_iso_duration("PT10S") == 10.0
    assert _parse_iso_duration("PT1H2M3.5S") == 3723.5
    assert _parse_iso_duration("P1DT1S") == 86401.0
    assert _parse_iso_duration(None) is None
    assert _parse_iso_duration("garbage") is None


def test_expand_timeline_negative_repeat_until_next_entry():
    entries = [(0, 2, -1), (10, 3, 0)]
    assert _expand_timeline(entries, None) == [
        (0, 2),
        (2, 2),
        (4, 2),
        (6, 2),
        (8, 2),
        (10, 3),
    ]


def test_expand_timeline_negative_repeat_until_period_end():
    # last entry repeats to the period end; a partial final segment still counts
    assert _expand_timeline([(0, 4, -1)], 10) == [(0, 4), (4, 4), (8, 4)]
    with pytest.raises(MediaError, match="r=-1"):
        _expand_timeline([(0, 4, -1)], None)


def test_parse_mpd_negative_repeat_uses_media_presentation_duration():
    xml = """<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT10S">
      <Period><AdaptationSet contentType="audio"><Representation id="a" bandwidth="1">
        <SegmentTemplate timescale="1000" startNumber="0" initialization="i.webm" media="s-$Number$.webm">
          <SegmentTimeline><S t="0" d="2000" r="-1"/></SegmentTimeline>
        </SegmentTemplate></Representation></AdaptationSet></Period></MPD>"""
    rep = parse_mpd(xml)[0]
    assert rep["timeline"] == [
        (0, 2000),
        (2000, 2000),
        (4000, 2000),
        (6000, 2000),
        (8000, 2000),
    ]
    assert duration_seconds(rep) == 10.0


def test_failed_download_leaves_no_workdir(tmp_path: Path):
    """Segment 403 -> MediaError, and the per-call temp dir is removed."""
    base, query = _manifest_base(MANIFEST_URL)
    files = {"playlistmultibitrate.mpd": MPD.encode()}  # no segments -> 403
    http = _FakeHTTP(files, query)
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg required for download_media preflight")
    with pytest.raises(MediaError, match="HTTP 403"):
        asyncio.run(media.download_media(http, MANIFEST_URL, tmp_path / "x.opus"))
    assert list(tmp_path.iterdir()) == []


def test_concurrent_downloads_use_distinct_workdirs(tmp_path: Path, monkeypatch):
    """Two in-flight calls to the same out_path must not share intermediates."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg required for download_media preflight")
    base, query = _manifest_base(MANIFEST_URL)
    seen: list[Path] = []
    gate = asyncio.Event()

    class SlowHTTP(_FakeHTTP):
        async def get(self, url, headers=None):
            if url.endswith(f"abc123-audio-init.webm?{query}"):
                seen.append(Path(url))
                await gate.wait()
            return await super().get(url, headers)

    real_mkdtemp = tempfile.mkdtemp
    dirs: list[str] = []

    def spy_mkdtemp(*a, **kw):
        d = real_mkdtemp(*a, **kw)
        dirs.append(d)
        return d

    monkeypatch.setattr(media.tempfile, "mkdtemp", spy_mkdtemp)
    files = {"playlistmultibitrate.mpd": MPD.encode()}
    http = SlowHTTP(files, query)

    async def run():
        t1 = asyncio.create_task(
            media.download_media(http, MANIFEST_URL, tmp_path / "x.opus")
        )
        t2 = asyncio.create_task(
            media.download_media(http, MANIFEST_URL, tmp_path / "x.opus")
        )
        while len(seen) < 2:
            await asyncio.sleep(0.01)
        assert len(set(dirs)) == 2
        gate.set()
        results = await asyncio.gather(t1, t2, return_exceptions=True)
        assert all(isinstance(r, MediaError) for r in results)  # 403 on init

    asyncio.run(run())
    assert list(tmp_path.iterdir()) == []
