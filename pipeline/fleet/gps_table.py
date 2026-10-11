"""F5: 2 Hz GPS table of every PIE trip -> _tmp/P-F/<scene_key>/trip.geojson (+ manifest.json for GPS-only trips).

  python pipeline/fleet/gps_table.py

States and gap_ms follow CONTRACT §3 (pie.gps_at); way_id, node_id, place_id and maxspeed_kmh come from map_match, whose
per-point residuals go to _work/<scene_key>/map_match.json. All trips share one replay clock: full trips first, then
GPS-only trips (trips.yaml order), back to back at rate 1.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common import lease  # noqa: E402
from pipeline.fleet import map_match, pie  # noqa: E402

STEP_FRAMES = pie.FPS // 2  # 2 Hz
# No way, junction or place from a position held or guessed, or too uncertain to tell a junction apart.
MAP_KEYS = ("way_id", "node_id", "place_id", "maxspeed_kmh", "residual_m", "road", "junction", "junction_m")


def trip_doc(vid: str, replay_start_ms: int) -> tuple[dict, list[dict]]:
    o, key, t1 = pie.obd(vid), pie.scene_key(vid), pie.t1_ms(vid)
    frames = range(0, len(o["lat"]), STEP_FRAMES)
    osm = pie.osm()
    pts, matches = [], []
    for f, g in zip(frames, pie.gps_at(o, frames)):
        stale = g["state"] in ("last_known", "unknown") or g["sigma_m"] > map_match.JUNCTION_M
        m = dict.fromkeys(MAP_KEYS) if stale else map_match.match(osm, g["lat"], g["lon"], g["heading"])
        lat, lon = round(g["lat"], 7), round(g["lon"], 7)
        pts.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
                    "properties": {"kind": "gps", "t_ms": pie.t_ms(f), "lat": lat, "lon": lon, "heading": round(g["heading"], 2),
                                   "speed": round(g["speed"], 3), "way_id": m["way_id"], "node_id": m["node_id"],
                                   "place_id": m["place_id"], "state": g["state"], "gap_ms": g["gap_ms"],
                                   "maxspeed_kmh": m["maxspeed_kmh"]}})
        matches.append({"t_ms": pie.t_ms(f), "state": g["state"], "sigma_m": round(g["sigma_m"], 1),
                        **{k: m[k] for k in ("residual_m", "road", "junction", "junction_m")}})
    replay = {"trip_id": key, "replay_start_ms": replay_start_ms, "source_start_ms": 0, "rate": 1.0, "coverage_intervals": [[0, t1]]}
    line = {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [p["geometry"]["coordinates"] for p in pts]},
            "properties": {"kind": "track", "trip_id": key, "replay": replay}}
    doc = {**pie.header([pie.obd_path(vid), pie.OSM_JSON]), "type": "FeatureCollection", "trip_id": key,
           "max_interpolation_gap_ms": pie.MAX_GAP_MS, "features": [line, *pts]}
    return doc, matches


def gps_only_manifest(vid: str, hdr: dict) -> dict:
    return {**hdr, "scene_id": pie.scene_id(vid), "scene_key": pie.scene_key(vid), "workspace": "fleet",
            "title": f"PIE {vid} (GPS only)", "t0_ms": 0, "t1_ms": pie.t1_ms(vid), "fps": pie.FPS, "world_frame_id": "wgs84",
            "metric": False, "cams": [], "assets": {"trip": "trip.geojson", "places": "places.json"},
            "sources": {"obd": pie.rel(pie.obd_path(vid)), "osm": pie.rel(pie.OSM_JSON)},
            "pipeline": {"models": {}, "created": pie.created()}}


def main() -> None:
    trips = yaml.safe_load(pie.TRIPS_YAML.read_text())
    full = [x["video"] for x in trips["full"]]
    replay = 0
    with lease.acquire(cpu=1, owner=pie.CARD):
        for vid in full + trips["gps_only"]:
            key = pie.scene_key(vid)
            doc, matches = trip_doc(vid, replay)
            out = pie.tmp_dir(key)
            pie.write_json(out / "trip.geojson", doc)
            res = np.array([m["residual_m"] for m in matches if m["residual_m"] is not None])
            med, p90 = (float(np.median(res)), float(np.percentile(res, 90))) if len(res) else (None, None)
            pie.write_json(pie.work_dir(key) / "map_match.json",
                           {"residual_m_median": med, "residual_m_p90": p90, "points": matches})
            if vid not in full:
                pie.write_json(out / "manifest.json", gps_only_manifest(vid, pie.header([out / "trip.geojson"])))
            states = [f["properties"]["state"] for f in doc["features"][1:]]
            print(f"{key}: {len(states)} pts, replay {replay}, last_known {states.count('last_known')}, "
                  f"unknown {states.count('unknown')}, matched {len(res)}"
                  + (f", residual median {med:.1f} m p90 {p90:.1f} m" if len(res) else ""), file=sys.stderr)
            replay += pie.t1_ms(vid)


if __name__ == "__main__":
    main()
