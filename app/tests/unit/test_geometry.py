import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from app.core import geo, geometry

ROOT = Path(__file__).resolve().parents[3] / 'fixtures/sva-out'


def test_ground_roundtrip():
    m = json.loads((ROOT / 'wh-sdg-fixture-01/manifest.json').read_text())
    for cam in m['cams']:
        p = np.array([0.2, -1.5, 0, 1.0])
        c = np.linalg.inv(np.array(cam['cam2world']).reshape(4, 4, order='F')) @ p
        fx, fy, cx, cy = cam['K']
        uv = [cx + fx*c[0]/-c[2], cy - fy*c[1]/-c[2]]
        assert np.linalg.norm(geometry.ground_point(cam, *uv) - p[:2]) < 1e-6
    assert geometry.distance({'x': 0, 'y': 0, 'sigma_m': .3}, {'x': 3, 'y': 4, 'sigma_m': .4}) == (5, .5)


def test_fisheye_roundtrip():
    K = np.array([[300., 0, 320], [0, 280, 180], [0, 0, 1]])
    D = np.array([-.02, .005, 0., 0.])
    xy = np.array([[.1, .2], [-.5, .3], [.7, -.4]], float)
    distorted = cv2.fisheye.distortPoints(xy.reshape(-1, 1, 2), K, D).reshape(-1, 2)
    uv = geometry.undistort_fisheye(distorted, K, D)
    expected = xy * [300, 280] + [320, 180]
    assert np.max(np.linalg.norm(uv - expected, axis=1)) < .1


def test_visibility_encoding_and_coverage():
    v = json.loads((ROOT / 'wh-sdg-fixture-01/visibility.json').read_text())
    states = geometry.floor_visibility(v, 4600, [.14, -1.5])
    assert states == {'ceiling_00': 'occluded', 'ceiling_04': 'visible', 'eye_00': 'visible'}
    cam = dict(id='top', K=[100, 100, 100, 100], w=200, h=200,
               cam2world=[1,0,0,0,0,1,0,0,0,0,1,0,0,0,10,1])
    cov = geometry.coverage([cam], [[-1,-1],[1,-1],[1,1],[-1,1]], [])
    assert cov['blind_m2'] == 0 and cov['area'] == pytest.approx(4)
    cov = geometry.coverage([cam], [[-1,-1],[1,-1],[1,1],[-1,1]],
                            [dict(xy=[0,0], radius_m=.4, height_m=3, body_radius_m=0)])
    assert cov['occluded_m2'] > 0


def test_geo_tables_facades_and_limit(tmp_path):
    from app.tests.unit.a5_fakes import fixture_store
    s = fixture_store(tmp_path)
    p = geo.geocode(s, 'Queen and Spadina')
    assert p['place_id'] == 'pl_queen_spadina'
    assert geo.find_passes(s, p['place_id'], 100, 120)
    assert geo.d2_to_ego(2, 8) == (8, -2)
    assert geo.facades(s, 'fl-pie-fixture-01', 2000)
    assert geo.speed_limit(s, 'fl-pie-fixture-01', 2000)['value'] == 40
    assert geo.speed_limit(s, 'fl-pie-fixture-02', 2000)['status'] == 'UNRESOLVED'
