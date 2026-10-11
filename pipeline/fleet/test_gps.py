"""GPS fix gating and map facts from stale positions.  .venv/bin/python -m pytest pipeline/fleet/test_gps.py -q"""
import numpy as np
import pytest

from pipeline.fleet import gps_table, map_match, pie

STEP = 30 / 3.6 / pie.FPS / pie.M_PER_DEG  # degrees of latitude per frame at 30 km/h


def _obd(lat, lon, speed_kmh):
    n = len(lat)
    return {"lat": np.asarray(lat, float), "lon": np.asarray(lon, float), "heading": np.zeros(n),
            "speed_kmh": np.full(n, float(speed_kmh))}


def _off_m(g, lat, lon):
    x0, y0 = pie.xy(g["lat"], g["lon"])
    x1, y1 = pie.xy(lat, lon)
    return float(np.hypot(x0 - x1, y0 - y1))


def test_a_jump_faster_than_the_car_drives_is_not_a_fix():
    n = 200
    lat = 43.67 + np.arange(n) * STEP
    lon = np.full(n, -79.40)
    lon[100:110] = -79.40 + 150 / (pie.M_PER_DEG * np.cos(np.radians(pie.CENTRE[0])))  # 150 m east, held 1/3 s
    lat[101:110] = lat[100]
    g = pie.gps_at(_obd(lat, lon, 30.0), [99, 100, 101, 105, 109, 110])
    assert g[1]["state"] != "observed"
    true_lat = 43.67 + np.array([99, 100, 101, 105, 109, 110]) * STEP
    assert all(_off_m(p, la, -79.40) < 5.0 for p, la in zip(g, true_lat))
    assert [p["state"] for p in g] == ["observed", "interpolated", "interpolated", "interpolated", "interpolated", "observed"]


def test_interpolated_sigma_grows_towards_the_middle_of_the_gap():
    lat = 43.67 + np.arange(200) * STEP
    lat[50:95] = lat[49]  # 1.5 s without a new fix at 30 km/h
    g = pie.gps_at(_obd(lat, np.full(200, -79.40), 30.0), [72])
    assert g[0]["state"] == "interpolated"
    assert g[0]["sigma_m"] == pytest.approx(pie.GPS_SIGMA_M + 30 / 3.6 * 23 / pie.FPS)  # 23 frames from either fix


def _osm():
    roads = [{"id": "w1", "name": "Bay Street", "maxspeed": "50", "coords": [[43.66, -79.40], [43.67, -79.40], [43.68, -79.40]]},
             {"id": "w2", "name": "King Street", "maxspeed": "40", "coords": [[43.67, -79.41], [43.67, -79.40], [43.67, -79.39]]}]
    a, b, of = [], [], []
    for i, r in enumerate(roads):
        for p, q in zip(r["coords"], r["coords"][1:]):
            a.append(pie.xy(*p)), b.append(pie.xy(*q)), of.append(i)
    return {"roads": roads, "road_a": np.array(a, float), "road_b": np.array(b, float), "road_of": np.array(of),
            "junctions": [(["Bay Street", "King Street"], (43.67, -79.40))], "junctions_xy": np.array([pie.xy(43.67, -79.40)], float),
            "crossings": [{"id": "n7", "lat": 43.67, "lon": -79.40}], "crossings_xy": np.array([pie.xy(43.67, -79.40)], float)}


def test_a_stale_position_near_a_junction_yields_no_map_facts(monkeypatch):
    n = 900
    lat = 43.67 - 30 / pie.M_PER_DEG + np.arange(n) * STEP
    lat[90:] = lat[89]  # the fix freezes 5 m south of the junction while the car keeps driving
    o = _obd(lat, np.full(n, -79.40), 30.0)
    monkeypatch.setattr(pie, "obd", lambda vid: o)
    monkeypatch.setattr(pie, "t1_ms", lambda vid: pie.t_ms(n))
    monkeypatch.setattr(pie, "osm", _osm)
    monkeypatch.setattr(pie, "header", lambda inputs: {})
    doc, matches = gps_table.trip_doc("set01_video_0001", 0)
    pts = {f["properties"]["t_ms"]: f["properties"] for f in doc["features"][1:]}
    mm = {m["t_ms"]: m for m in matches}
    stale = pts[pie.t_ms(600)]
    assert stale["state"] == "last_known"
    assert stale["place_id"] is None and stale["way_id"] is None and stale["node_id"] is None and stale["maxspeed_kmh"] is None
    m = mm[pie.t_ms(600)]
    assert m["junction"] is None and m["road"] is None and m["state"] == "last_known" and m["sigma_m"] > map_match.JUNCTION_M
    near = pts[pie.t_ms(75)]  # observed, 9 m before the junction
    assert near["state"] == "observed" and near["place_id"] == "pl_bay_street_king_street" and near["node_id"] == 7
    assert mm[pie.t_ms(75)]["junction"] == ["Bay Street", "King Street"] and mm[pie.t_ms(75)]["state"] == "observed"


def test_map_facts_need_a_fresh_and_tight_position():
    ok = {"state": "interpolated", "sigma_m": map_match.JUNCTION_M}
    assert map_match.trusted(ok)
    assert not map_match.trusted({**ok, "sigma_m": map_match.JUNCTION_M + 0.1})
    assert not any(map_match.trusted({**ok, "state": s}) for s in ("last_known", "unknown"))
