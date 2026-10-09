"""Street (L1) manifests from live rows.  .venv/bin/python -m pytest pipeline/street/test_l1_scenes.py -q"""
import json
import sys

import pytest

from pipeline.common import schema_check
from pipeline.street import l1_scenes

SF4 = "s3://team-38-vss-chunks/team-38/20261001_0600{:02d}_sf4_chunk_{:04d}.mp4"


def seg(chunk, k, end=None, stem="sf4", camera_id="sf_streets_cam-4", location="san_francisco", uri=SF4):
    """Live row of segment k of a 30 s chunk; end = the chunk length when it is the last, shorter chunk."""
    start = 5.0 * k
    return {"source": f"s3://team-38-vss-chunks-segments/segments/{stem}_{chunk:04d}_segment_{k:03d}.mp4",
            "original_video": uri.format(chunk, chunk), "segment_start_sec": start,
            "segment_end_sec": min(start + 5.0, end or 30.0), "camera_id": camera_id, "location": location,
            "extra_metadata": json.dumps({"chunk_index": chunk, "chunk_start_sec": 30.0 * chunk,
                                          "stream_position_sec": 30.0 * chunk + start}),
            "upload_timestamp": None, "reasoning_content": f"{stem} chunk {chunk} segment {k}"}


def sf4_rows():
    """3 chunks, the last 12.5 s long, newest chunk first."""
    return [seg(2, k, end=12.5) for k in range(3)] + [seg(c, k) for c in (1, 0) for k in reversed(range(6))]


def other_rows():
    i24 = "s3://team-38-vss-chunks/team-38/20261001_0552{:02d}_scene1_p1c1_chunk_{:04d}.mp4"
    ss = "s3://team-38-vss-chunks/team-38/20261001_0758{:02d}_2025_test_Warehouse_017_Camera_chunk_{:04d}.mp4"
    return [seg(0, 0, stem="scene1_p1c1", camera_id="i24_cam-1", location="nashville", uri=i24),
            seg(0, 0, stem="ss", camera_id="smartspace_cam-1", location="indoor", uri=ss)]


def test_street_rows_become_one_scene_per_camera_and_video():
    nb = "s3://team-38-vss-chunks/team-38/20261001_0700{:02d}_neighborhood_20260901_chunk_{:04d}.mp4"
    rows = sf4_rows() + other_rows() + [seg(0, k, stem="neighborhood_20260901", camera_id="neighborhood_cam-1",
                                            location="neighborhood", uri=nb) for k in range(6)]
    built = l1_scenes.scenes(rows)
    assert list(built) == ["st-neighborhood_cam_1-neighborhood_20260901", "st-sf_streets_cam_4-sf4"]
    m, caps = built["st-sf_streets_cam_4-sf4"]
    assert (m["scene_id"], m["workspace"], m["evidence_level"], m["title"]) == (
        "st/sf_streets_cam_4/sf4", "street", "L1", "San Francisco street camera 4 (sf4)")
    assert m["sources"] == {"vss": {"camera_id": "sf_streets_cam-4", "location": "san_francisco", "stem": "sf4"}}
    cam = m["cams"][0]
    assert (cam["kind"], cam["id"], cam["w"], cam["h"], cam["video"], cam["thumbs"]) == ("cam2d", "sf_streets_cam-4", 1920, 1080, None, None)
    assert [(c["t0_ms"], c["t1_ms"], c["uri"]) for c in cam["chunks"]] == [
        (0, 30000, SF4.format(0, 0)), (30000, 60000, SF4.format(1, 1)), (60000, 72500, SF4.format(2, 2))]
    assert m["t1_ms"] == 72500
    assert len(caps) == 15
    assert caps[0] == {"clip_id": "st-sf_streets_cam_4-sf4:sf_streets_cam-4:00000", "caption": "sf4 chunk 0 segment 0",
                       "prompt_rev": "vss", "model_rev": "nvidia/cosmos3-nano-reasoner", "analyzed_ms": None}
    assert caps[-1]["clip_id"] == "st-sf_streets_cam_4-sf4:sf_streets_cam-4:00014"
    nm, ncaps = built["st-neighborhood_cam_1-neighborhood_20260901"]
    assert (nm["cams"][0]["id"], nm["t1_ms"], len(ncaps)) == ("neighborhood_cam-1", 30000, 6)
    assert (nm["cams"][0]["w"], nm["cams"][0]["h"], nm["fps"], nm["cams"][0]["frame_ref"]["thumb_h"]) == (2048, 1154, 19.9, 180)


def test_a_missing_chunk_is_an_error():
    rows = [r for r in sf4_rows() if "chunk_0001" not in r["original_video"]]
    with pytest.raises(ValueError, match="starts at 60000 ms, the chunks before it end at 30000 ms"):
        l1_scenes.scenes(rows)


def test_manifest_validates_against_the_street_schema():
    """needs T1: schemas/manifest.json with workspace street and the cam2d camera."""
    m, _ = l1_scenes.scenes(sf4_rows())["st-sf_streets_cam_4-sf4"]
    assert schema_check.schema_errors(m, "manifest.json") == []


def test_cli_writes_manifests_and_captions_from_a_row_dump(tmp_path, monkeypatch, capsys):
    dump = tmp_path / "rows.json"
    dump.write_text(json.dumps(sf4_rows() + other_rows()))
    out, cache = tmp_path / "sva-out", tmp_path / "cache"
    monkeypatch.setattr(sys, "argv", ["l1_scenes.py", "--rows-json", str(dump), "--sva-out", str(out), "--cache", str(cache)])
    l1_scenes.main()
    m = json.loads((out / "st-sf_streets_cam_4-sf4/manifest.json").read_text())
    assert m["scene_key"] == "st-sf_streets_cam_4-sf4"
    lines = (cache / "st-sf_streets_cam_4-sf4/captions.jsonl").read_text().splitlines()
    assert [json.loads(x)["clip_id"][-5:] for x in lines] == [f"{i:05d}" for i in range(15)]
    assert sorted(p.parent.name for p in out.glob("*/manifest.json")) == ["st-sf_streets_cam_4-sf4"]
    err = capsys.readouterr().err
    assert "l1_scenes: 1 street scenes, 3 chunks, 15 segments from 17 live rows" in err
    assert "st-sf_streets_cam_4-sf4: chunks=3 segments=15 t1_ms=72500" in err
