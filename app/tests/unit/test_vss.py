import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.core.vss import VSS

RESPONSE = json.loads((Path(__file__).resolve().parents[3] / 'fixtures/vss/search_response.json').read_text())


def fake_backend(seen):
    def respond(req):
        seen.append(req)
        if req.url.path.endswith('/auth/login'):
            return httpx.Response(200, json={'access_token': 'fixture'})
        return httpx.Response(200, json=RESPONSE)
    return respond


def test_fixture_mapping_login_and_sql():
    seen = []
    settings = SimpleNamespace(vss_url='http://vss', vss_username='fixture', vss_password='fixture', site_map_path='unused')
    first = RESPONSE['results'][0]['source']
    mapper = lambda hit: ('wh-sdg-fixture-01', 'ceiling_04', int(hit['segment_start_sec'] * 1000)) if hit['source'] == first else None
    vss = VSS(settings, map_hit=mapper, client=httpx.Client(transport=httpx.MockTransport(fake_backend(seen))))
    out = vss.search('worker steps back', 10, {'time_filter': {'time_filter': '24h'}})
    assert out['sql_query'] == RESPONSE['sql_query']
    assert [h['rank'] for h in out['hits']] == list(range(1, len(RESPONSE['results']) + 1))
    assert out['hits'][0]['scene_key'] == 'wh-sdg-fixture-01' and out['hits'][0]['source'] == first
    assert all(h['scene_key'] is None and h['cam_id'] is None and h['t_ms'] is None for h in out['hits'][1:])
    assert all(len(h['caption']) <= 200 and isinstance(h['similarity'], float) for h in out['hits'])
    body = json.loads(seen[-1].content)
    assert seen[-1].url.path == '/api/v1/search'
    assert 'hybrid_text_weight' not in body and body['time_filter'] == '24h'
    assert seen[-1].headers['authorization'] == 'Bearer fixture'


def test_search_asks_for_one_llm_summary_and_waits_30_s(monkeypatch):
    seen, timeouts = [], []
    client = httpx.Client
    def make(**kw):
        timeouts.append(kw['timeout'])
        return client(transport=httpx.MockTransport(fake_backend(seen)), **kw)
    monkeypatch.setattr(httpx, 'Client', make)
    settings = SimpleNamespace(vss_url='http://vss', vss_username='fixture', vss_password='fixture')
    VSS(settings, map_hit=lambda hit: None).search('worker steps back', 10, {})
    assert json.loads(seen[-1].content)['llm_top_n'] == 1
    assert timeouts and all(t >= 30 for t in timeouts)


def test_search_logs_in_again_on_401():
    seen, tokens = [], iter(['old', 'new'])
    def respond(req):
        seen.append(req)
        if req.url.path.endswith('/auth/login'):
            return httpx.Response(200, json={'access_token': next(tokens)})
        if req.headers['authorization'] == 'Bearer old':
            return httpx.Response(401)
        return httpx.Response(200, json={'results': []})
    settings = SimpleNamespace(vss_url='http://vss', vss_username='fixture', vss_password='fixture')
    vss = VSS(settings, map_hit=lambda hit: None, client=httpx.Client(transport=httpx.MockTransport(respond)))
    vss.search('a car', 5, {})
    assert seen[-1].method == 'POST' and seen[-1].url.path == '/api/v1/search' and seen[-1].headers['authorization'] == 'Bearer new'
    assert [r.url.path for r in seen].count('/api/v1/auth/login') == 2


@pytest.mark.parametrize('behaviour', ['hang', 'raise'])
def test_site_retrieve_does_not_wait_for_vss(tmp_path, monkeypatch, behaviour):
    from app.api.search import SearchRequest, retrieve
    from app.core import embed
    from app.tests.unit.a5_fakes import fixture_store
    def search(*a):
        if behaviour == 'raise':
            raise httpx.ConnectError('vss down')
        time.sleep(60)
    monkeypatch.setattr(embed, 'embed_text', lambda q: [1.0] * 256)
    store = fixture_store(tmp_path)
    started = time.monotonic()
    _, r = retrieve(store, SearchRequest(q='When did the worker start to dodge the forklift?', workspace='warehouse'), vss=SimpleNamespace(search=search))
    assert time.monotonic() - started < 1
    assert r.envelope.output['moments'] and 'sql_query' not in r.envelope.output
