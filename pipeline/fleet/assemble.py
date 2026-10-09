"""Full trip: every step into _tmp/P-F/<scene_key>/, then publish. Run gps_table.py first (trip.geojson, map_match.json).

  python pipeline/fleet/assemble.py SCENE_KEY

Steps: video + thumbs (F2), boxes (F3), entities, pose2d (F4), ego (F7, F8 facades), buildings (F8), events (F9),
timeline, manifest, publish. The video, boxes and pose2d steps are skipped when their output exists.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common import detect_track, lease, publish, video  # noqa: E402
from pipeline.fleet import building_info, ego_scene, events, pie, pose2d  # noqa: E402

CLASSES = ["person", "car", "truck", "bus", "bicycle"]
VIDEO_MAX_W = 1280


def entities(key: str, rows: list[list]) -> dict:
    """One entity per local track: P<n> for people, V<n> for vehicles, numbered by first appearance."""
    tracks: dict[int, list[list]] = {}
    for r in rows:
        tracks.setdefault(r[1], []).append(r)
    n = {"person": 0, "vehicle": 0}
    ents = []
    for tid, rs in sorted(tracks.items(), key=lambda kv: (kv[1][0][0], kv[0])):
        kind = "person" if rs[0][2] == "person" else "vehicle"
        n[kind] += 1
        t0, t1 = rs[0][0], min(rs[-1][0] + 100, pie.t1_ms(pie.vid_of(key)))
        ents.append({"entity_id": f"{'P' if kind == 'person' else 'V'}{n[kind]}", "kind": kind,
                     "observations": [{"cam_id": pie.CAM, "local_track_id": tid, "t0_ms": t0, "t1_ms": t1, "method": "bytetrack",
                                       "conf": round(sum(r[7] for r in rs) / len(rs), 3)}],
                     "association": {"method": "ground_dist", "conf": 1.0, "valid": [t0, t1]}})
    return {"entities": ents}


def timeline(rows: list[list], evs: list[dict], t1: int) -> dict:
    """Second s counts the tracks boxed at s*1000+500; the dashcam lane is ok wherever the video runs."""
    at: dict[int, list[str]] = {}
    for r in rows:
        at.setdefault(r[0], []).append(r[2])
    sec = []
    for s in range(math.ceil(t1 / 1000)):
        cls = at.get(s * 1000 + 500, [])
        sec.append([s, cls.count("person"), len(cls) - cls.count("person"), int(s * 1000 + 500 < t1),
                    [e["id"] for e in evs if e["t0_ms"] < (s + 1) * 1000 and e["t1_ms"] > s * 1000]])
    return {"sec": sec, "lanes": {pie.CAM: [[0, t1, "ok"]]}}


def manifest(key: str, hdr: dict, vinfo: dict, thumb_wh: tuple[int, int]) -> dict:
    vid, cal = pie.vid_of(key), pie.calibration()
    (fx, _, cx), (_, fy, cy), _ = cal["K"]
    w, h = cal["dim"]
    cam = {"id": pie.CAM, "w": w, "h": h, "K": [fx, fy, cx, cy], "model": "fisheye", "dist": [d[0] for d in cal["D"]],
           "camera_basis": "opengl", "cam2world": ego_scene.cam2world(cal),
           "fov_deg": round(math.degrees(2 * math.atan(h / (2 * fy))), 6), "video": f"video/{pie.CAM}.mp4",
           "frame_ref": {"video_w": vinfo["w"], "video_h": vinfo["h"], "thumb_w": thumb_wh[0], "thumb_h": thumb_wh[1]},
           "offset_ms": 0, "thumbs": f"thumbs/{pie.CAM}", "sync_method": "single_camera", "sync_residual_ms": 0.0}
    return {**hdr, "scene_id": pie.scene_id(vid), "scene_key": key, "workspace": "fleet", "title": f"PIE {vid}",
            "t0_ms": 0, "t1_ms": pie.t1_ms(vid), "fps": pie.FPS, "world_frame_id": "wgs84+ego_flu", "metric": True,
            "cams": [cam],
            "assets": {"trip": "trip.geojson", "ego_dir": "ego", "buildings": "buildings.json", "entities": "entities.json",
                       "events": "events.json", "timeline": "timeline.json", "boxes_dir": "boxes", "places": "places.json"},
            "sources": {"video": pie.rel(pie.clip(vid)), "obd": pie.rel(pie.obd_path(vid)),
                        "calibration": pie.rel(pie.PIE / "calibration_data.json"), "osm": pie.rel(pie.OSM_JSON),
                        "ego_frame": "x forward, y left, z up; origin on the ground below the camera"},
            "pipeline": {"models": {"detector": "yolo11m", "tracker": "bytetrack", "pose": "rtmpose-m body7 256x192"},
                         "created": pie.created()}}


def run(key: str) -> Path:
    vid, out = pie.vid_of(key), pie.tmp_dir(key)
    src, mp4, thumbs = pie.clip(vid), out / "video" / f"{pie.CAM}.mp4", out / "thumbs" / pie.CAM
    if not mp4.exists():
        with lease.acquire(cpu=4, owner=pie.CARD):
            video.transcode(src, mp4, max_w=VIDEO_MAX_W)
            video.thumbs(mp4, thumbs)
    if not (out / "boxes" / pie.CAM).is_dir():
        detect_track.run(src, out, pie.CAM, CLASSES, analysis_rev=pie.ANALYSIS_REV, input_name=pie.rel(src), owner=pie.CARD)
    rows = pie.load_boxes(out)
    pie.write_json(out / "entities.json", {**pie.header([src]), **entities(key, rows)})
    if not (pie.work_dir(key) / "pose2d.json").exists():
        pose2d.run(key)
    ego_scene.run(key)
    building_info.run(key)
    events.run(key)
    with lease.acquire(cpu=1, owner=pie.CARD):
        evs = json.loads((out / "events.json").read_text())["events"]
        pie.write_json(out / "timeline.json", {**pie.header([out / "events.json"]), **timeline(rows, evs, pie.t1_ms(vid))})
        vinfo = video.probe(mp4)
        thumb_wh = Image.open(next(iter(sorted(thumbs.glob("*.jpg"))))).size
        pie.write_json(out / "manifest.json", manifest(key, pie.header([src, pie.obd_path(vid)]), vinfo, thumb_wh))
        return publish.publish(pie.CARD, key)


if __name__ == "__main__":
    print(run(sys.argv[1]))
