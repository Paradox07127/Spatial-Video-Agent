"""F1: choose the full trips and write manifests/trips.yaml.

  python pipeline/fleet/select_trips.py --n 6

The PIE videos in the VSS corpus (data/manifest.jsonl) come first, in corpus order; the remaining places go to the videos
that add a criterion the chosen ones miss: a pedestrian crossing at an intersection (PIE attributes), shop signs along
the route (OSM shops near the GPS track), hard braking (OBD). The other videos are GPS-only trips.
"""
from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common import lease  # noqa: E402
from pipeline.fleet import pie  # noqa: E402

SHOP_M = 30.0  # a shop this close to the route has its sign in view
SHOPS_MIN = 10
CRITERIA = {
    "intersection_crossing": lambda f: f["intersection_crossings"] > 0,
    "shop_signs": lambda f: f["shops_near_route"] >= SHOPS_MIN,
    "hard_brake": lambda f: f["min_accel_mps2"] <= pie.HARD_BRAKE_MPS2,
}


def corpus_videos() -> list[str]:
    rows = [json.loads(line) for line in (pie.DATA / "manifest.jsonl").open()]
    rows = sorted((r["wallclock_start"], r["dataset_video_id"]) for r in rows if r["dataset"] == "PIE")
    return list(dict.fromkeys(v for _, v in rows))


def features(vid: str, shops_xy: np.ndarray) -> dict:
    peds = ET.parse(pie.attr_path(vid)).getroot().iter("pedestrian")
    crossings = sum(p.get("crossing") == "1" and p.get("intersection") != "midblock" for p in peds)
    o = pie.obd(vid)
    g = [p for p in pie.gps_at(o, range(0, len(o["lat"]), pie.FPS // 2)) if p["state"] != "unknown"]
    route = np.stack(pie.xy([p["lat"] for p in g], [p["lon"] for p in g]), 1)
    near = sum(bool((np.hypot(*(route - s).T) <= SHOP_M).any()) for s in shops_xy)
    return {"intersection_crossings": int(crossings), "shops_near_route": int(near),
            "min_accel_mps2": round(float(pie.accel(o).min()), 2)}


def select(feats: dict[str, dict], corpus: list[str], n: int) -> list[str]:
    chosen = [v for v in corpus if v in feats][:n]
    while len(chosen) < n:
        missing = [c for c, ok in CRITERIA.items() if not any(ok(feats[v]) for v in chosen)]
        rest = [v for v in feats if v not in chosen]
        chosen.append(max(rest, key=lambda v: (sum(CRITERIA[c](feats[v]) for c in missing),
                                               sum(ok(feats[v]) for ok in CRITERIA.values()),
                                               feats[v]["intersection_crossings"])))
    return chosen


def main(n: int) -> None:
    with lease.acquire(cpu=1, owner=pie.CARD):
        o = pie.osm()
        shops_xy = o["pois_xy"][[p["key"] == "shop" for p in o["pois"]]]
        feats = {v: features(v, shops_xy) for v in pie.video_ids()}
    corpus = corpus_videos()
    full = select(feats, corpus, n)
    doc = {"full": [{"video": v, "scene_key": pie.scene_key(v), "corpus": v in corpus,
                     "criteria": [c for c, ok in CRITERIA.items() if ok(feats[v])], **feats[v]} for v in full],
           "gps_only": [v for v in feats if v not in full]}
    pie.TRIPS_YAML.write_text(f"# select_trips.py --n {n}: full trips (corpus first, then missing criteria) and GPS-only trips\n"
                              + yaml.safe_dump(doc, sort_keys=False))
    for x in doc["full"]:
        print(x, file=sys.stderr)
    print(f"{len(doc['full'])} full, {len(doc['gps_only'])} gps-only -> {pie.TRIPS_YAML}", file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=6)
    main(ap.parse_args().n)
