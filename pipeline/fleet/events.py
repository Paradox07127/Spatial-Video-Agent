"""F9: rule events of a trip -> events.json.

  python pipeline/fleet/events.py SCENE_KEY

ped_near: a pedestrian on the ground within NEAR_M ahead and inside the ego lane (ego frames).
ped_crossing: a pedestrian ahead whose lateral position crosses the ego lane (CROSS_RULE).
hard_brake: OBD deceleration at or below pie.HARD_BRAKE_MPS2.
intersection_pass: the car within JUNCTION_M of an OSM junction (map-match of the 2 Hz GPS points).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common import lease  # noqa: E402
from pipeline.fleet import map_match, pie  # noqa: E402

NEAR_M = 5.0
LANE_HALF_M = 1.75  # half of a 3.5 m lane
MOVING_MPS = 2.8  # 10 km/h: a pedestrian close ahead of a car standing at a light is not a conflict
JOIN_MS = 1000  # detections of one episode further apart than this start a new one
JUNCTION_M = map_match.JUNCTION_M
BRAKE_SEVERE_MPS2 = -4.5
BRAKE_EMERGENCY_MPS2 = -6.0
CROSS_AHEAD_M = 20.0
CROSS_WINDOW_MS = 4000
CROSS_FAST_KMH = 10.0
CROSS_NEAR_M = 10.0
CROSS_MIN_TRAVEL_M = 1.0  # well above the single-view lateral sigma (often > 0.3 m), so y jitter around 0 is no crossing
CROSS_RULE = (f"pedestrian 0 < x <= {CROSS_AHEAD_M:g} m ahead whose y goes from |y| > {LANE_HALF_M} m to the other side, or "
              f"across y = 0 inside the lane moving >= {CROSS_MIN_TRAVEL_M:g} m in y, within {CROSS_WINDOW_MS / 1000:g} s; "
              f"severity 2 when the OBD speed is >= "
              f"{CROSS_FAST_KMH:g} km/h and x <= {CROSS_NEAR_M:g} m at the crossing, else 1")


def _episodes(ts: list[int], gap: int) -> list[tuple[int, int]]:
    out = []
    for t in sorted(ts):
        if out and t - out[-1][1] <= gap:
            out[-1][1] = t
        else:
            out.append([t, t])
    return [tuple(x) for x in out]


def _crosses(a: float, b: float) -> bool:
    outside = abs(a) > LANE_HALF_M and abs(b) > LANE_HALF_M
    inside = abs(a) <= LANE_HALF_M and abs(b) <= LANE_HALF_M and abs(b - a) >= CROSS_MIN_TRAVEL_M
    return a * b < 0 and (outside or inside)


def ped_crossings(frames: list[dict], speed, t1: int) -> list[dict]:
    """ped_crossing events of ego frames by CROSS_RULE; speed(t_ms) -> OBD m/s. Crossings of one pedestrian less than
    JOIN_MS apart form one event with the highest severity."""
    seen: dict[str, list[tuple[int, float, float]]] = {}
    for fr in frames:
        for p in fr["peds"]:
            if 0 < p["x"] <= CROSS_AHEAD_M:
                seen.setdefault(p["entity_id"], []).append((fr["t_ms"], float(p["x"]), float(p["y"])))
    out = []
    for eid, s in seen.items():
        eps: list[dict] = []
        start = 0
        for j, (tj, _, yj) in enumerate(s):
            i = next((i for i in range(start, j) if tj - s[i][0] <= CROSS_WINDOW_MS and _crosses(s[i][2], yj)), None)
            if i is None:
                continue
            tk, xk, _ = next(q for q in s[i + 1:j + 1] if (q[2] < 0) != (s[i][2] < 0))
            v = speed(tk) * 3.6
            e = {"type": "ped_crossing", "t0_ms": s[i][0], "t1_ms": min(tj + 100, t1), "actor_ids": [eid],
                 "severity": 2 if v >= CROSS_FAST_KMH and xk <= CROSS_NEAR_M else 1,
                 "params": {"rule": CROSS_RULE, "y_from_m": round(s[i][2], 2), "y_to_m": round(yj, 2),
                            "x_at_cross_m": round(xk, 2), "ego_speed_kmh": round(v, 1), "t_cross_ms": tk}}
            if eps and e["t0_ms"] - eps[-1]["t1_ms"] <= JOIN_MS:
                worse = e if e["severity"] > eps[-1]["severity"] else eps[-1]
                eps[-1] = {**worse, "t0_ms": eps[-1]["t0_ms"], "t1_ms": e["t1_ms"]}
            else:
                eps.append(e)
            start = j + 1
        out += eps
    return out


def run(key: str) -> Path:
    vid, out, work = pie.vid_of(key), pie.tmp_dir(key), pie.work_dir(key)
    with lease.acquire(cpu=1, owner=pie.CARD):
        o, t1 = pie.obd(vid), pie.t1_ms(vid)
        gps = [f["properties"] for f in json.loads((out / "trip.geojson").read_text())["features"][1:]]
        gps_t = np.array([p["t_ms"] for p in gps])
        place = lambda t: gps[int(np.clip(np.searchsorted(gps_t, t, "right") - 1, 0, len(gps) - 1))]["place_id"]  # noqa: E731
        speed = lambda t: float(o["speed_kmh"][min(round(t * pie.FPS / 1000), len(o["speed_kmh"]) - 1)] / 3.6)  # noqa: E731
        ev = []

        near: dict[str, list[tuple[int, float]]] = {}
        frames = [fr for f in sorted((out / "ego").glob("*.json")) for fr in json.loads(f.read_text())["frames"]]
        for fr in frames:
            for p in fr["peds"]:
                if 0 < p["x"] <= NEAR_M and abs(p["y"]) <= LANE_HALF_M:
                    near.setdefault(p["entity_id"], []).append((fr["t_ms"], float(np.hypot(p["x"], p["y"]))))
        for eid, hits in near.items():
            for a, b in _episodes([t for t, _ in hits], JOIN_MS):
                d = min(x for t, x in hits if a <= t <= b)
                v = max(speed(t) for t, _ in hits if a <= t <= b)
                ev.append({"type": "ped_near", "t0_ms": a, "t1_ms": min(b + 100, t1), "actor_ids": [eid],
                           "severity": (3 if d < NEAR_M / 2 else 2) if v >= MOVING_MPS else 1,
                           "params": {"min_dist_m": round(d, 2), "max_ego_speed_mps": round(v, 2), "lane_half_m": LANE_HALF_M}})
        ev += ped_crossings(frames, speed, t1)

        acc = pie.accel(o)
        hard = [pie.t_ms(i) for i in np.nonzero(acc <= pie.HARD_BRAKE_MPS2)[0]]
        for a, b in _episodes(hard, JOIN_MS):
            i0, i1 = round(a * pie.FPS / 1000), round(b * pie.FPS / 1000)
            amin = float(acc[i0:i1 + 1].min())
            ev.append({"type": "hard_brake", "t0_ms": a, "t1_ms": min(b + pie.t_ms(1), t1), "actor_ids": [],
                       "severity": 3 if amin <= BRAKE_EMERGENCY_MPS2 else 2 if amin <= BRAKE_SEVERE_MPS2 else 1,
                       "params": {"decel_mps2": round(amin, 2), "v0_mps": round(speed(a), 2), "v1_mps": round(speed(b), 2)}})

        mm = json.loads((work / "map_match.json").read_text())["points"]
        at: dict[tuple, list[tuple[int, float]]] = {}
        for m in mm:
            if m["junction"] and m["junction_m"] <= JUNCTION_M:
                at.setdefault(tuple(m["junction"]), []).append((m["t_ms"], m["junction_m"]))
        for names, hits in at.items():
            for a, b in _episodes([t for t, _ in hits], 1000):
                ev.append({"type": "intersection_pass", "t0_ms": a, "t1_ms": min(b + 500, t1), "actor_ids": [], "severity": 0,
                           "params": {"roads": list(names), "min_dist_m": min(d for t, d in hits if a <= t <= b),
                                      "min_speed_mps": round(min(speed(t) for t, _ in hits if a <= t <= b), 2)}})

        ev.sort(key=lambda e: (e["t0_ms"], e["type"]))
        events = [{"id": f"E{i + 1}", "type": e["type"], "t0_ms": e["t0_ms"], "t1_ms": e["t1_ms"], "actor_ids": e["actor_ids"],
                   "place_id": place(e["t0_ms"]), "severity": e["severity"], "source": "rule", "params": e["params"],
                   "clip_ids": [f"{key}:{pie.CAM}:{c:05d}" for c in range(e["t0_ms"] // 5000, (e["t1_ms"] - 1) // 5000 + 1)],
                   "eids": []} for i, e in enumerate(ev)]
        pie.write_json(out / "events.json", {**pie.header([pie.obd_path(vid), out / "trip.geojson", work / "map_match.json"]),
                                             "events": events})
    counts = {t: sum(e["type"] == t for e in events) for t in ("ped_near", "ped_crossing", "hard_brake", "intersection_pass")}
    print(f"events {key}: {counts}, severity>=2: {sum(e['severity'] >= 2 for e in events)}", file=sys.stderr)
    return out / "events.json"


if __name__ == "__main__":
    run(sys.argv[1])
