import json
import subprocess
import sys

from pipeline.common import video
from pipeline.street import transcode_chunks


def _stream(path, kind):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", kind, "-show_entries",
                          "stream=codec_name,pix_fmt,start_time", "-of", "json", str(path)], capture_output=True, check=True).stdout
    return json.loads(out)["streams"][0]


def test_hevc_chunks_become_h264_in_place_with_audio_and_video_start_kept(tmp_path, monkeypatch):
    out, backup = tmp_path / "sva-out", tmp_path / "backup"
    chunk_dir = out / "st-x" / "chunks" / "cam-1"
    chunk_dir.mkdir(parents=True)
    hevc, h264 = chunk_dir / "0000.mp4", chunk_dir / "0001.mp4"
    # video starts 0.2 s after the audio, like the VSS chunks
    subprocess.run([*video.FF, "-itsoffset", "0.2", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=2", "-f", "lavfi",
                    "-i", "sine=duration=2.2", "-c:v", "libx265", "-pix_fmt", "yuv420p10le", "-c:a", "aac", str(hevc)], check=True)
    subprocess.run([*video.FF, "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=2", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", str(h264)], check=True)
    h264_bytes = h264.read_bytes()
    (out / "st-x" / "manifest.json").write_text(json.dumps({"scene_key": "st-x", "workspace": "street", "cams": [
        {"kind": "cam2d", "id": "cam-1", "chunks": [{"t0_ms": 0, "t1_ms": 2000, "uri": "chunks/cam-1/0000.mp4"},
                                                     {"t0_ms": 2000, "t1_ms": 4000, "uri": "chunks/cam-1/0001.mp4"}]}]}))
    v_start = float(_stream(hevc, "v")["start_time"])
    monkeypatch.setattr(sys, "argv", ["transcode_chunks.py", "--sva-out", str(out), "--backup", str(backup)])
    transcode_chunks.main()
    v = _stream(hevc, "v")
    assert (v["codec_name"], v["pix_fmt"]) == ("h264", "yuv420p")
    assert _stream(hevc, "a")["codec_name"] == "aac"
    assert abs(float(v["start_time"]) - v_start) < 0.05
    assert abs(video.probe(hevc)["duration_ms"] - 2200) < 100
    assert _stream(backup / "st-x" / "chunks" / "cam-1" / "0000.mp4", "v")["codec_name"] == "hevc"
    assert h264.read_bytes() == h264_bytes and not (backup / "st-x" / "chunks" / "cam-1" / "0001.mp4").exists()
    transcode_chunks.main()
    assert _stream(hevc, "v")["codec_name"] == "h264"
