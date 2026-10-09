"""Fleet geometry and GPS state rules.  .venv/bin/python -m pytest pipeline/fleet/test_fleet.py -q"""
import numpy as np
import pytest
from shapely.geometry import shape

from pipeline.fleet import building_info, ego_scene, facades, pie

# raw/pie/calibration_data.json
CAL = {"cam_height_mm": 1270, "cam_pitch_deg": -10, "dim": [1920, 1080],
       "K": [[1004.8374471951423, 0.0, 960.1025514993675], [0.0, 1004.3912782107128, 573.5538287373604], [0.0, 0.0, 1.0]],
       "D": [[-0.02748054291929438], [-0.007055051080370751], [-0.039625194298025156], [0.019310795479533783]]}


def test_d2_right_forward_becomes_ego_forward_left():
    assert ego_scene.flu(2.0, 10.0) == (10.0, -2.0)
    assert ego_scene.flu(-3.5, 7.0) == (7.0, 3.5)


@pytest.mark.parametrize("x,y", [(5.0, 0.0), (12.0, 0.0), (30.0, 0.0), (6.0, 4.0), (10.0, -6.0), (25.0, 8.0), (4.0, -3.0)])
def test_fisheye_pixel_back_to_the_same_ground_point(x, y):
    uv = ego_scene.project_ego(np.array([[x, y, 0.0]]), CAL)
    fl = ego_scene.ground_flu(uv, CAL)
    assert np.allclose(fl, [[x, y]], atol=1e-3)


def test_ground_point_ahead_lies_below_the_horizon_on_the_centre_column():
    uv = ego_scene.project_ego(np.array([[20.0, 0.0, 0.0]]), CAL)[0]
    assert abs(uv[0] - 960.1) < 0.5 and 573.6 < uv[1] < 1080
    assert np.isnan(ego_scene.ground_flu(np.array([[960.0, 300.0]]), CAL)).all()  # above the horizon: no ground hit


def test_cam2world_is_camera_to_ego_in_opengl_basis():
    m = np.array(ego_scene.cam2world(CAL)).reshape(4, 4, order="F")
    assert np.allclose(m[:3, 3], [0, 0, 1.27])
    fwd = -m[:3, 2]
    assert fwd[0] > 0.98 and abs(fwd[1]) < 1e-9 and fwd[2] > 0  # PIE pitch -10 deg looks 10 deg up
    assert np.allclose(m[:3, 0], [0, -1, 0])


def test_facades_skip_the_building_a_gps_fix_falls_inside():
    sq = lambda x0, x1, y0, y1: np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]], float)  # noqa: E731
    rings = [sq(-5, 5, -5, 5), sq(-10, 10, 30, 40)]  # east, north metres from pie.CENTRE
    o = {"bld_rings": rings, "bld_c": np.array([r[:-1].mean(0) for r in rings]),
         "bld_r": np.array([np.hypot(*(r - r[:-1].mean(0)).T).max() for r in rings]),
         "buildings": [{"id": "w1"}, {"id": "w2"}]}
    seen = facades.facades(o, pie.CENTRE[0], pie.CENTRE[1], 0.0, CAL)
    assert set(seen) == {"w2"} and seen["w2"][2] == pytest.approx(30.0, abs=1.0)


def _sq(x0, x1, y0, y1, cw=False):
    r = [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]
    return r[::-1] if cw else r


def test_footprint_keeps_a_hole_with_rfc7946_winding():
    # Outer ring clockwise, hole counterclockwise: both the wrong way round.
    g = building_info.footprint({"type": "MultiPolygon", "coordinates": [[_sq(0, 10, 0, 10, cw=True), _sq(3, 6, 3, 6)]]})
    s = shape(g)
    assert g["type"] == "Polygon" and s.is_valid and s.area == pytest.approx(91)
    assert s.exterior.is_ccw and len(s.interiors) == 1 and not s.interiors[0].is_ccw


def test_footprint_merges_overlapping_parts_and_keeps_the_hole_of_a_separate_part():
    g = building_info.footprint({"type": "MultiPolygon", "coordinates": [
        [_sq(0, 10, 0, 10)], [_sq(5, 15, 0, 10)], [_sq(20, 30, 0, 10), _sq(22, 24, 2, 4)]]})
    s = shape(g)
    assert g["type"] == "MultiPolygon" and s.is_valid and len(s.geoms) == 2 and s.area == pytest.approx(150 + 96)
    assert sorted(len(p.interiors) for p in s.geoms) == [0, 1]
    assert all(p.exterior.is_ccw and all(not r.is_ccw for r in p.interiors) for p in s.geoms)


def test_footprint_splits_a_self_intersecting_ring():
    s = shape(building_info.footprint({"type": "MultiPolygon", "coordinates": [[[[0, 0], [10, 10], [10, 0], [0, 10], [0, 0]]]]}))
    assert s.is_valid and s.geom_type == "MultiPolygon" and s.area == pytest.approx(50)


def _obd(lat, speed_kmh):
    n = len(lat)
    return {"lat": np.asarray(lat, float), "lon": np.full(n, -79.40), "heading": np.zeros(n),
            "speed_kmh": np.asarray(speed_kmh, float)}


def test_gps_states_follow_the_contract_gap_rule():
    fps = pie.FPS
    t = pie.t_ms
    lat = 43.67 + np.arange(300) * 1e-5  # a new fix every frame while driving 36 km/h
    lat[30:45] = lat[29]  # 0.5 s without a new fix: interpolated
    lat[100:190] = lat[99]  # 3 s frozen while moving: last_known
    lat[250:260] = 0.0  # invalid fixes, 0.33 s: interpolated
    o = _obd(lat, np.full(300, 36.0))
    g = pie.gps_at(o, [0, 40, 99, 150, 189, 255])
    assert [p["state"] for p in g] == ["observed", "interpolated", "observed", "last_known", "last_known", "interpolated"]
    assert g[0]["gap_ms"] == 0 and g[1]["gap_ms"] == t(40) - t(29)
    assert g[3]["gap_ms"] == t(150) - t(99) and g[3]["lat"] == lat[99]
    assert g[3]["sigma_m"] == pytest.approx(pie.GPS_SIGMA_M + 10.0 * 51 / fps)
    assert lat[249] < g[5]["lat"] < lat[260]


def test_standing_still_keeps_the_fix_observed():
    lat = np.full(200, 43.67)
    g = pie.gps_at(_obd(lat, np.zeros(200)), [0, 100, 199])
    assert [p["state"] for p in g] == ["observed"] * 3


def test_constant_speed_has_no_acceleration_at_the_ends():
    a = pie.accel(_obd(np.full(300, 43.67), np.full(300, 34.0)))
    assert np.abs(a).max() < 1e-9


def test_braking_shows_as_deceleration():
    v = np.r_[np.full(100, 36.0), np.linspace(36.0, 0.0, 61), np.zeros(100)]  # 10 m/s to 0 in 2 s
    a = pie.accel(_obd(np.full(len(v), 43.67), v))
    assert a.min() == pytest.approx(-5.0, abs=0.3)


def test_points_before_the_first_fix_are_unknown():
    lat = np.r_[np.zeros(20), 43.67 + np.arange(40) * 1e-5]
    g = pie.gps_at(_obd(lat, np.full(60, 18.0)), [5, 20])
    assert g[0]["state"] == "unknown" and g[0]["lat"] == lat[20] and g[1]["state"] == "observed"


def test_provenance_names_a_staged_input_by_its_published_path():
    assert pie.rel(pie.OUT / '_tmp' / 'M2' / 'fl-pie-set04_video_0007' / 'trip.geojson') == 'fl-pie-set04_video_0007/trip.geojson'
    assert pie.rel(pie.OUT / 'geo' / 'pie_osm.json') == 'geo/pie_osm.json'
    assert pie.rel(pie.DATA / 'raw' / 'osm' / 'ontario-latest.osm.pbf') == 'raw/osm/ontario-latest.osm.pbf'
