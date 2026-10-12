"""Fleet ped_crossing rule and landmark places.  .venv/bin/python -m pytest pipeline/fleet/test_events_places.py -q"""
import contextlib
import json
import math

import numpy as np
import pytest

from pipeline.fleet import events, pie, places


def _frames(peds: dict) -> list[dict]:
    """10 fps ego frames over 10 s; peds: entity_id -> t_ms -> (x, y) or None."""
    out = []
    for t in range(0, 10000, 100):
        ps = [{"entity_id": e, "x": p[0], "y": p[1]} for e, f in peds.items() if (p := f(t)) is not None]
        out.append({"t_ms": t, "peds": ps})
    return out


def _across(x, t0, t1):
    """At x, from y = +3 to y = -3 between t0 and t1."""
    return lambda t: (x, 3.0 - 6.0 * min(max((t - t0) / (t1 - t0), 0.0), 1.0))


def test_ped_crossing_rule_and_severity():
    peds = {"P1": _across(8.0, 1000, 4000), "P2": _across(15.0, 1000, 4000), "P3": lambda t: (8.0, 3.0),
            "P4": _across(-5.0, 1000, 4000), "P5": _across(25.0, 1000, 4000), "P6": _across(8.0, 6000, 9000)}
    speed_mps = lambda t: (20.0 if t < 5000 else 5.0) / 3.6  # noqa: E731
    evs = events.ped_crossings(_frames(peds), speed_mps, 10000)
    assert {e["actor_ids"][0]: e["severity"] for e in evs} == {"P1": 2, "P2": 1, "P6": 1}
    p1 = next(e for e in evs if e["actor_ids"] == ["P1"])
    assert p1["type"] == "ped_crossing" and 1000 <= p1["t0_ms"] < p1["t1_ms"] <= 4100 and "rule" in p1["params"]
    assert p1["params"]["x_at_cross_m"] == 8.0 and p1["params"]["ego_speed_kmh"] == 20.0


def test_ped_crossing_inside_lane_needs_lateral_travel():
    peds = {"J": lambda t: (8.0, 0.2 if t // 100 % 2 else -0.2), "M": lambda t: (8.0, 0.6 if t < 3000 else -0.6)}
    evs = events.ped_crossings(_frames(peds), lambda t: 0.0, 10000)
    assert [e["actor_ids"] for e in evs] == [["M"]]


V_TURN, W_TURN = 15 / 3.6, math.radians(15)  # 15 km/h at 15 deg/s


def _turn(w):
    """Ego pose (x, y, yaw) on a circle at V_TURN and yaw rate w (rad/s), starting at the origin along +x."""
    return lambda t: (V_TURN / w * math.sin(w * t / 1000), V_TURN / w * (1 - math.cos(w * t / 1000)), w * t / 1000)


def _ego_of(pose, t, P):
    """World point P -> ego frame at t."""
    ex, ey, a = pose(t)
    dx, dy = P[0] - ex, P[1] - ey
    return math.cos(a) * dx + math.sin(a) * dy, -math.sin(a) * dx + math.cos(a) * dy


def _world_of(pose, t, p):
    ex, ey, a = pose(t)
    return ex + math.cos(a) * p[0] - math.sin(a) * p[1], ey + math.sin(a) * p[0] + math.cos(a) * p[1]


@pytest.mark.parametrize("w", [W_TURN, -W_TURN])
def test_stationary_pedestrians_while_the_ego_turns_do_not_cross(w):
    pose = _turn(w)
    peds = {f"S{x}_{y}": (lambda P: lambda t: _ego_of(pose, t, P))((x, y))
            for x in (6, 10, 14, 18, 20) for y in (2.5, 4, 6, 8, -2.5, -4, -6, -8)}
    assert events.ped_crossings(_frames(peds), lambda t: V_TURN, 10000, pose) == []


@pytest.mark.parametrize("w", [W_TURN, -W_TURN])
def test_pedestrian_crossing_the_path_of_a_turning_ego_is_a_crossing(w):
    pose, d = _turn(w), 1 if w > 0 else -1
    # 1.5 m/s across the lane line of the ego at 4 s, 10 m ahead; it never changes side in the raw ego frames
    walk = lambda t: _ego_of(pose, t, _world_of(pose, 4000, (10.0, d * (2.5 - 1.5 * (4000 - t) / 1000))))  # noqa: E731
    evs = events.ped_crossings(_frames({"C": lambda t: walk(t) if 1000 <= t <= 4000 else None}), lambda t: V_TURN, 10000, pose)
    assert [e["actor_ids"] for e in evs] == [["C"]]
    p = evs[0]["params"]
    assert d * p["y_from_m"] < -events.LANE_HALF_M and d * p["y_to_m"] > events.LANE_HALF_M and p["x_at_cross_m"] == pytest.approx(walk(p["t_cross_ms"])[0], abs=0.01)


def test_ego_pose_dead_reckons_obd_heading_clockwise_from_north():
    n = pie.FPS + 1
    east = events.ego_pose({"heading": np.full(n, 90.0), "speed_kmh": np.full(n, 36.0)})(1000)
    north = events.ego_pose({"heading": np.zeros(n), "speed_kmh": np.full(n, 36.0)})(1000)
    assert east == pytest.approx((10.0, 0.0, 0.0), abs=1e-6) and north == pytest.approx((0.0, 10.0, math.pi / 2), abs=1e-6)
    right = events.ego_pose({"heading": np.linspace(0.0, 15.0, n), "speed_kmh": np.full(n, 15.0)})(1000)
    assert right[2] < math.pi / 2 and right[0] > 0  # a right turn lowers the CCW yaw and drifts east


def test_run_ignores_obd_heading_jitter_of_a_stopped_car(tmp_path, monkeypatch):
    """A stopped car whose OBD heading flips 8 deg per frame and a pedestrian standing 10 m ahead: no ped_crossing."""
    out, work, n = tmp_path / "out", tmp_path / "work", 10 * pie.FPS
    (out / "ego").mkdir(parents=True)
    work.mkdir()
    (out / "trip.geojson").write_text(json.dumps({"features": [{}, {"properties": {"t_ms": 0, "place_id": "p1"}}]}))
    (out / "ego" / "000.json").write_text(json.dumps({"frames": _frames({"S": lambda t: (10.0, 0.4)})}))
    (work / "map_match.json").write_text(json.dumps({"points": []}))
    o = {"heading": 8.0 * (np.arange(n) % 2), "speed_kmh": np.zeros(n)}
    for name, f in {"vid_of": lambda k: k, "tmp_dir": lambda k: out, "work_dir": lambda k: work, "obd": lambda v: o,
                    "t1_ms": lambda v: 10000, "obd_path": lambda v: tmp_path, "header": lambda inputs: {}}.items():
        monkeypatch.setattr(pie, name, f)
    monkeypatch.setattr(events.lease, "acquire", lambda **kw: contextlib.nullcontext())
    evs = json.loads(events.run("fl-pie-test").read_text())["events"]
    assert [e["type"] for e in evs if e["type"] == "ped_crossing"] == []


def test_landmark_rows_near_the_trip_with_osm_aliases():
    pts = [{"lat": 43.6677 + i * 1e-5, "lon": -79.3955} for i in range(20)]  # ~60 m west of the museum outline
    ring = [[-79.3950, 43.6672], [-79.3940, 43.6672], [-79.3940, 43.6682], [-79.3950, 43.6682], [-79.3950, 43.6672]]
    lms = [{"id": "w4942687", "name": "Royal Ontario Museum", "aliases": ["ROM"],
            "geom": {"type": "MultiPolygon", "coordinates": [[ring]]}},
           {"id": "n1", "name": "Far Park", "aliases": [], "geom": {"type": "Point", "coordinates": [-79.40, 43.68]}}]
    rows = places.landmark_places(pts, lms)
    assert [r["place_id"] for r in rows] == ["pl_lm_w4942687"]
    r = rows[0]
    assert r["kind"] == "landmark" and r["aliases"] == ["ROM"] and abs(r["lat"] - 43.6677) < 1e-4 and abs(r["lon"] + 79.3945) < 1e-4
    assert r["geom"]["type"] == "MultiPolygon"
