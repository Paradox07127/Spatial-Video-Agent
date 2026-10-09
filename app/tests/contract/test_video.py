"""video.py against the fixture videos (ffmpeg on PATH): probe, thumbnails keyed by scene second, transcode, frames."""
import shutil

import pytest

from pipeline.common import video

from .conftest import SVA_OUT, WH_KEY

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg missing")
SRC = SVA_OUT / WH_KEY / "video/ceiling_04.mp4"


def test_probe():
    p = video.probe(SRC)
    assert (p["w"], p["h"], p["n_frames"], p["codec"]) == (640, 360, 296, "h264") and abs(p["fps"] - 30) < 1e-6


def test_thumbs_follow_offset(tmp_path):
    names = [p.name for p in video.thumbs(SRC, tmp_path, offset_ms=120)]
    assert names == [f"{s:05d}.jpg" for s in range(10)]


def test_transcode_and_frames(tmp_path):
    dst = video.transcode(SRC, tmp_path / "x.mp4", max_w=320)
    assert video.probe(dst)["w"] == 320
    jpgs = video.frames_at(dst, [0, 5000])
    assert all(b[:2] == b"\xff\xd8" for b in jpgs)
