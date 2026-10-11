import copy
import json

from app.tests.unit.test_api import client, street_app, WH, FL, index_db
from app.tests.unit.test_http_functional import health_client


def test_pixel_zone_on_an_uncalibrated_street_camera_is_refused(tmp_path, monkeypatch):
    from app.tests.unit.a5_fakes import L1_KEY, L1_CAM
    with street_app(tmp_path, monkeypatch, site=False) as c:
        r = c.post(f'/api/scene/{L1_KEY}/zone', json={'cam': L1_CAM, 'polygon_px': [[10, 10], [100, 10], [50, 80]]})
        assert r.status_code == 422


def test_zone_request_is_validated_at_the_boundary(client):
    store = client.app.state.store
    m = copy.deepcopy(store.manifest(WH))
    m['cams'][0].update(model='fisheye', dist=[0.0, 0.0, 0.0, 0.0])
    store._manifests[WH] = m
    cam = m['cams'][0]['id']
    square = [[-7, -4], [7, -4], [7, 4], [-7, 4]]
    bad = [{'cam': cam, 'polygon_px': []}, {'cam': cam, 'polygon_px': [[1, 1], [2, 2]]},
           {'polygon_world': square, 't_range': [5000, 4000]}, {'polygon_world': square, 't_range': [4000, 4000]},
           {'polygon_world': [[0, 0], [1, 1], [1, 0], [0, 1]]}]
    for body in bad:
        assert client.post(f'/api/scene/{WH}/zone', json=body).status_code == 422, body
    nan = '{"polygon_world": [[0, 0], [NaN, 0], [1, 1]]}'
    assert client.post(f'/api/scene/{WH}/zone', content=nan, headers={'content-type': 'application/json'}).status_code == 422
    assert client.post(f'/api/scene/{WH}/zone', json={'polygon_world': square, 't_range': [0, 10000]}).status_code == 200


def test_fleet_place_comes_from_an_observed_gps_sample_at_the_frame(client):
    gap = client.get(f'/api/fleet/trip/{FL}/moment?t=6500').json()
    assert gap['t_ms'] == 6500 and gap['place'] is None
    fix = client.get(f'/api/fleet/trip/{FL}/moment?t=7000').json()
    assert fix['place']['place_id'] == 'pl_queen_spadina'
    assert client.get(f'/api/fleet/trip/{FL}/moment?t=4100').json()['place']['place_id'] == 'pl_queen_st_w'


def test_fleet_moment_rejects_a_stale_ego_frame(client, monkeypatch):
    store = client.app.state.store
    real = store.asset_json

    def holed(key, rel):
        doc = real(key, rel)
        return {**doc, 'frames': [f for f in doc['frames'] if not 5000 <= f['t_ms'] < 6000]} if key == FL and '/ego/' in f'/{rel}' else doc
    monkeypatch.setattr(store, 'asset_json', holed)
    assert client.get(f'/api/fleet/trip/{FL}/moment?t=5500').status_code == 404
    assert client.get(f'/api/fleet/trip/{FL}/moment?t=6040').json()['t_ms'] == 6000


def test_health_rejects_a_mirror_with_another_analysis_rev(health_client):
    client, _, m, root, _ = health_client
    m['analysis_rev'] = 'r2'
    (root / 'wh-test/manifest.json').write_text(json.dumps({'analysis_rev': 'r1'}))
    assert client.get('/health').json()['assets_ok'] is False
    (root / 'wh-test/manifest.json').write_text(json.dumps({'analysis_rev': 'r2', 'pad': 1}))
    assert client.get('/health').status_code == 200
