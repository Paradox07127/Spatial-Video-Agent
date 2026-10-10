"""Live VSS row naming -> our scenes.  .venv/bin/python -m pytest pipeline/common/test_site_map.py -q"""
import gzip
import json
from datetime import datetime

import pytest

from pipeline.common import site_map

SDG = "s3://team-38-vss-chunks/team-38/20261001_074538_0851b7a332077a4fc0e2_01021d3989e38beb3395_run_10_seed_213384163.ceiling_01.rgb_chunk_0000.mp4"
SS = "s3://team-38-vss-chunks/team-38/20261001_075838_2025_test_Warehouse_017_Camera{}_chunk_0002.mp4"
PIE_KEY = "team-38/20261001_061422_set01_video_chunk_0021.mp4"
IDENTITY = {"set01_video": [{"chunk_index": 21, "s3_key": PIE_KEY, "video_id": "set01/video_0002", "offset_s": 25.0, "preroll_s_from_container": 5.167}]}


def row(original_video, segment_start_sec, stream_position_sec, source="s3://seg.mp4", camera_id="cam-1"):
    return {"source": source, "original_video": original_video, "segment_start_sec": segment_start_sec, "camera_id": camera_id,
            "extra_metadata": json.dumps({"chunk_start_sec": stream_position_sec - segment_start_sec,
                                          "stream_position_sec": stream_position_sec}),
            "upload_timestamp": datetime(2026, 10, 1, 7, 45, 12)}


def test_sdg_stem_maps_second_hash_run_and_camera():
    assert site_map.scene_of(row(SDG, 5.0, 5.0), {}) == ("sdg", ("wh-sdg-01021d39_run_10", "ceiling_01", 5000))


def test_smartspaces_cameras_and_stream_position_already_includes_the_segment():
    assert site_map.scene_of(row(SS.format(""), 5.0, 65.0), {}) == ("smartspaces", ("wh-ss-warehouse_017", "camera", 65000))
    assert site_map.scene_of(row(SS.format("_02"), 0.0, 60.0), {}) == ("smartspaces", ("wh-ss-warehouse_017", "camera_02", 60000))


def test_pie_chunk_maps_through_live_identity():
    pie = site_map.pie_index(IDENTITY)
    assert site_map.scene_of(row("s3://team-38-vss-chunks/" + PIE_KEY, 5.0, 635.0), pie) == ("pie", ("fl-pie-set01_video_0002", "dashcam", 30000))
    missing = "s3://team-38-vss-chunks/team-38/20261001_061422_set01_video_chunk_0022.mp4"
    assert site_map.scene_of(row(missing, 5.0, 665.0), pie) == ("pie", None)


@pytest.mark.parametrize("stem, camera_id, key", [
    ("VID_20261006_174811_003", "nyc_streets_cam-1", "st-nyc_streets_cam_1-vid_20261006_174811_003"),
    ("IMG_0509", "nyc_streets_cam-2", "st-nyc_streets_cam_2-img_0509"),
    ("sf1", "sf_streets_cam-1", "st-sf_streets_cam_1-sf1"),
    ("neighborhood_20260901", "neighborhood_cam-1", "st-neighborhood_cam_1-neighborhood_20260901"),
])
def test_street_stems_map_to_their_camera_scene(stem, camera_id, key):
    chunk = f"s3://team-38-vss-chunks/team-38/20261008_074640_{stem}_chunk_0003.mp4"
    assert site_map.scene_of(row(chunk, 25.0, 115.0, camera_id=camera_id), {}) == ("street", (key, camera_id, 115000))


@pytest.mark.parametrize("stem", ["GX050001", "GOPR0130", "GX010001"])
def test_removed_camera_stems_have_no_scene(stem):
    chunk = f"s3://team-38-vss-chunks/team-38/20261008_074640_{stem}_chunk_0003.mp4"
    assert site_map.scene_of(row(chunk, 25.0, 115.0, camera_id="nyc_bike_gopro-1"), {}) == ("other", None)


def test_other_sources_have_no_scene():
    i24 = "s3://team-38-vss-chunks/team-38/20261001_055247_scene1_p1c1_chunk_0000.mp4"
    assert site_map.scene_of(row(i24, 0.0, 0.0, camera_id="i24_cam-1"), {}) == ("other", None)


def test_build_hits_published_cameras_only(tmp_path):
    scene = tmp_path / "wh-ss-warehouse_017"
    scene.mkdir()
    (scene / "manifest.json").write_text(json.dumps({"cams": [{"id": "camera"}, {"id": "camera_01"}]}))
    street = tmp_path / "st-sf_streets_cam_4-sf4"
    street.mkdir()
    (street / "manifest.json").write_text(json.dumps({"cams": [{"id": "sf_streets_cam-4"}]}))
    sf4 = "s3://team-38-vss-chunks/team-38/20261001_060000_sf4_chunk_0001.mp4"
    rows = [row(SS.format(""), 5.0, 65.0, "s3://a.mp4"), row(SS.format("_02"), 5.0, 65.0, "s3://b.mp4"), row(SDG, 0.0, 0.0, "s3://c.mp4"),
            row(sf4, 10.0, 40.0, "s3://d.mp4", camera_id="sf_streets_cam-4")]
    doc = site_map.build(rows, tmp_path, {})
    a, b, c, d = (doc["map"][k] for k in ("s3://a.mp4", "s3://b.mp4", "s3://c.mp4", "s3://d.mp4"))
    assert (a["hit"], a["clip_id"], a["t_ms"], a["upload_ms"]) == (True, "wh-ss-warehouse_017:camera:00013", 65000, 1790840712000)
    assert (b["hit"], b["scene_key"]) == (False, "wh-ss-warehouse_017")
    assert (c["hit"], c["scene_key"]) == (False, "wh-sdg-01021d39_run_10")
    assert (d["hit"], d["clip_id"]) == (True, "st-sf_streets_cam_4-sf4:sf_streets_cam-4:00008")
    assert doc["families"]["smartspaces"] == {"rows": 2, "named": 2, "published": 2, "hit": 1}
    assert doc["families"]["street"] == {"rows": 1, "named": 1, "published": 1, "hit": 1}
    assert (doc["n_rows"], doc["n_hit"]) == (4, 2)
    (tmp_path / "site_map.json").write_text(json.dumps(doc))
    maps = site_map.load(tmp_path / "site_map.json")
    moved = {"source": "s3://moved.mp4", "original_video": SS.format(""), "segment_start_sec": 5.0}
    assert site_map.map_hit(maps, moved)["clip_id"] == "wh-ss-warehouse_017:camera:00013"
    (tmp_path / "site_map.json.gz").write_bytes(gzip.compress(json.dumps(doc).encode()))
    assert site_map.load(tmp_path / "site_map.json.gz") == maps
