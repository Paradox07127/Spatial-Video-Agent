import contextlib
import json
import subprocess
import sys

from pipeline.common import embed, video


def test_clips_without_a_corpus_segment_embed_a_cut_of_their_own_video(tmp_path, monkeypatch):
    out, clip_dir = tmp_path / "sva-out", tmp_path / "data" / "sva_clips"
    sdir = out / "fl-pie-x"
    (sdir / "video").mkdir(parents=True)
    subprocess.run([*video.FF, "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=12", "-c:v", "libx264", "-g", "30",
                    "-pix_fmt", "yuv420p", str(sdir / "video" / "dashcam.mp4")], check=True)
    (sdir / "manifest.json").write_text(json.dumps({"scene_key": "fl-pie-x", "t0_ms": 0, "t1_ms": 12000,
                                                    "cams": [{"id": "dashcam", "video": "video/dashcam.mp4", "offset_ms": 0}]}))
    site_map = tmp_path / "site_map.json"
    site_map.write_text(json.dumps({"map": {"s0": {"clip_id": "fl-pie-x:dashcam:00000", "segment_path": "/corpus/seg_00.mp4"}}}))
    sent = []
    monkeypatch.setattr(embed, "embed_videos", lambda paths: sent.extend(paths) or [[0.0] * embed.DIM for _ in paths])
    monkeypatch.setattr(embed.lease, "acquire", lambda **kw: contextlib.nullcontext())
    argv = ["embed.py", "--sva-out", str(out), "--cache", str(tmp_path / "cache"), "--site-map", str(site_map),
            "--clip-dir", str(clip_dir)]
    monkeypatch.setattr(sys, "argv", argv)
    embed.main()
    own = [clip_dir / "fl-pie-x" / "dashcam-00001.mp4", clip_dir / "fl-pie-x" / "dashcam-00002.mp4"]
    assert sent == ["/corpus/seg_00.mp4", *map(str, own)]
    assert [video.probe(p)["duration_ms"] for p in own] == [5000, 2000]
    rows = [json.loads(x) for x in (tmp_path / "cache" / "fl-pie-x" / "embeds.jsonl").read_text().splitlines()]
    assert {r["clip_id"]: r["src"] for r in rows} == {"fl-pie-x:dashcam:00000": "/corpus/seg_00.mp4",
                                                      "fl-pie-x:dashcam:00001": str(own[0]), "fl-pie-x:dashcam:00002": str(own[1])}
    sent.clear()
    embed.main()
    assert sent == []
