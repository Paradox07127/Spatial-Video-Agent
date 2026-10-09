"""Fleet ped_crossing rule and landmark places.  .venv/bin/python -m pytest pipeline/fleet/test_events_places.py -q"""
from pipeline.fleet import events, places


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
