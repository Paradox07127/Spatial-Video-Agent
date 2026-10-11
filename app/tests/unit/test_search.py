import json

import httpx
import pytest

from app.core import embed, llm
from app.api.search import SearchRequest, event_types, retrieve
from app.tests.unit.a5_fakes import fixture_store
from pipeline.common.schema_check import schema_errors

QUESTIONS = [
    ('When did the worker start to dodge the forklift, and how far was it?', 'find'),
    ('Is there anyone riding a bicycle in the warehouse?', 'find'), ('List accident clips', 'find'),
    ('Find workers near a forklift', 'find'), ('Show pedestrian crossings', 'find'),
    ('Where is P1?', 'where_is'), ('Where is F1?', 'where_is'),
    ('Near the Queen and Spadina junction, did any pedestrian cross in front of the car?', 'where_is'),
    ('Near ROM, did any pedestrian cross?', 'where_is'), ('What is that building on the right?', 'where_is'),
    ('What happened?', 'what_happened'), ('What happened before the brake?', 'what_happened'),
    ('Explain this accident', 'what_happened'), ('Describe this event', 'what_happened'),
    ('Is this the same person in these two clips?', 'link'), ('Link P1 across cameras', 'link'),
    ('Did P1 enter before F1?', 'link'), ('Who is the person in eye_00?', 'unsupported'),
    ('Why did the driver do this deliberately?', 'unsupported'), ('Whose fault is the accident?', 'unsupported'),
]


@pytest.mark.parametrize('q,expected', QUESTIONS)
def test_router(q, expected):
    assert llm.rule_parse(q, {'workspace': 'warehouse'}).template == expected


@pytest.mark.parametrize('q', ['When does a pedestrian cross the street?', 'How many people walk past in this clip?',
                               'When is the street busiest in this clip?'])
def test_street_presets_route_to_find(q):
    assert llm.rule_parse(q, {'workspace': 'street'}).template == 'find'


@pytest.mark.parametrize('q,workspace,template,entities,place,t_range', [
    ('When did the worker start to dodge the forklift, and how far was it?', 'warehouse', 'find', ['person','forklift'], None, None),
    ('Is there anyone in the ceiling_00 blind spot from 0 to 10 seconds?', 'warehouse', 'find', ['person'], None, (0,10000)),
    ("Is there anyone where ceiling_00 can't see?", 'warehouse', 'find', ['person'], None, None),
    ('What is the closest approach of the worker and forklift?', 'warehouse', 'find', ['person','forklift'], None, None),
    ('At Queen and Spadina, what is the building on the right?', 'fleet', 'where_is', [], 'Queen & Spadina', None),
    ('Which building is on the left at Queen & Spadina?', 'fleet', 'where_is', [], 'Queen & Spadina', None),
    ('Was this car speeding?', 'fleet', 'find', ['vehicle'], None, None),
    ('Link P1 across cameras', 'warehouse', 'link', ['P1'], None, None),
    ('Where is P2 from 2 to 5 s?', 'warehouse', 'where_is', ['P2'], None, (2000,5000)),
    ('What happened?', 'warehouse', 'what_happened', [], None, None),
    ('Who is the person in eye_00?', 'warehouse', 'unsupported', ['person'], None, None),
    ('What is the worker trying to do?', 'warehouse', 'unsupported', ['person'], None, None),
    ('What is the name of the worker?', 'warehouse', 'unsupported', ['person'], None, None),
    ('Who caused the accident?', 'warehouse', 'unsupported', [], None, None),
])
def test_english_question_types(q, workspace, template, entities, place, t_range):
    parsed = llm.rule_parse(q, {'workspace':workspace})
    assert (parsed.template, parsed.entities, parsed.place, parsed.t_range)==(template, entities, place, t_range)
    assert parsed.abstain_reason==('unsupported_question' if template=='unsupported' else None)


@pytest.mark.parametrize('q,expected', [
    ('When did the worker dodge the forklift?', {'dodge'}),
    ('When did the worker yield?', {'dodge'}),
    ('When did the worker give way?', {'dodge'}),
    ('Did the pedestrian cross?', {'ped_crossing'}),
    ('Was there hard braking?', {'hard_brake'}),
    ('Was there a near miss?', {'near_miss'}),
    ('How far was the worker from the forklift?', {'near_miss'}),
])
def test_english_event_keywords(q, expected):
    assert event_types(q)==expected


def test_entities_time_and_llm_fallback(monkeypatch):
    q = llm.rule_parse('Find P1 and F1 from 2 to 5 seconds', {'workspace': 'warehouse'})
    assert q.entities == ['P1', 'F1'] and q.t_range == (2000, 5000)
    monkeypatch.setenv('LLM_URL', 'http://llm/v1')
    monkeypatch.setattr(llm, 'complete_json', lambda *a, **k: {'entities': 7})
    assert llm.parse('Find P1', {'workspace': 'warehouse'}).entities == ['P1']
    assert llm.parse('Anything odd with the forklift?', {'workspace': 'warehouse'}).template == 'unsupported'
    calls = []
    def complete(*a, **k):
        calls.append(k['role'])
        return {'template': 'find', 'entities': ['F1']}
    monkeypatch.setattr(llm, 'complete_json', complete)
    assert llm.parse('Find a worker', {'workspace': 'warehouse'}).entities == ['person'] and calls == []
    assert llm.parse('Who is that person?', {'workspace': 'warehouse'}).template == 'unsupported' and calls == []
    q = llm.parse('Anything odd with the forklift?', {'workspace': 'warehouse'})
    assert (q.template, q.entities, q.abstain_reason, calls) == ('find', ['F1'], None, ['parse'])


@pytest.mark.parametrize('port', [8103, 8003])
def test_embed_bodies(monkeypatch, port):
    seen = []
    def respond(req):
        seen.append((req.url.path, json.loads(req.content)))
        return httpx.Response(200, json={'embeddings': [[0.1] * 256]} if port == 8103 else {'data': [{'embedding': [0.1] * 256}]})
    monkeypatch.setenv('EMBED_URL', f'http://embed:{port}')
    client = httpx.Client(transport=httpx.MockTransport(respond))
    assert len(embed.embed_text('worker', client=client)) == 256
    assert seen[0][0] == ('/embed/text' if port == 8103 else '/v1/embeddings')
    assert seen[0][1] == {'texts': ['worker']} if port == 8103 else seen[0][1]['request_type'] == 'query'
    with pytest.raises(ValueError):
        embed.validate_vector([0.0] * 255)


def test_search_group_expansion_and_schema(tmp_path, monkeypatch):
    store = fixture_store(tmp_path)
    monkeypatch.setattr(embed, 'embed_text', lambda q: [0.0] * 256)
    parsed, result = retrieve(store, SearchRequest(q='The worker dodges the forklift', workspace='warehouse'))
    cards = result.envelope.output['moments']
    assert len(cards) == 1 and cards[0]['t0_ms'] == 4600
    assert cards[0]['cams'] == ['ceiling_04','eye_00'] and cards[0]['primary_cam']=='ceiling_04'
    assert result.envelope.output['expanded_ranges'][0]['t1_ms'] == 10000
    from app.api.search import search_response
    assert schema_errors(search_response(store, parsed, result), 'search.json') == []


def test_request_scope_and_workspace():
    from pydantic import ValidationError
    assert SearchRequest(q='worker', workspace='warehouse').scope == 'scene'
    assert SearchRequest(q='worker', scope='corpus').workspace is None
    assert SearchRequest(q='car', workspace='street', scene_id='st/sf_streets_cam_4/sf4').scene_id == 'st/sf_streets_cam_4/sf4'
    for bad in [{'q': 'worker'}, {'q': 'worker', 'scope': 'everything'}, {'q': 'worker', 'workspace': 'ocean'}]:
        with pytest.raises(ValidationError):
            SearchRequest(**bad)


def test_corpus_search_route_without_workspace(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.tests.unit.a5_fakes import L1_KEY, l1_assets
    index, assets = l1_assets(tmp_path)
    for key, value in {'MODE': 'dev', 'DATA_BACKEND': 'duckdb', 'INDEX_DUCKDB': str(index), 'APP_DUCKDB': str(tmp_path / 'app.duckdb'),
                       'RUNS_DIR': str(tmp_path / 'runs'), 'ASSET_DIR': str(assets)}.items():
        monkeypatch.setenv(key, value)
    from app.main import create_app
    with TestClient(create_app()) as c:
        cards = c.post('/api/search', json={'q': 'Find a person', 'scope': 'corpus'}).json()['cards']
        assert {card['scene_key'] for card in cards} >= {'wh-sdg-fixture-01', 'fl-pie-fixture-01', L1_KEY}
        assert c.post('/api/search', json={'q': 'Find a person'}).status_code == 422


def test_no_match_unsupported_and_hard_time(tmp_path, monkeypatch):
    store = fixture_store(tmp_path)
    monkeypatch.setattr(embed, 'embed_text', lambda q: [0.0] * 256)
    for q in ['Is there anyone riding a bicycle in the warehouse?', 'Who is the person in eye_00?']:
        _, r = retrieve(store, SearchRequest(q=q, workspace='warehouse'))
        assert r.envelope.output['moments'] == []
    _, r = retrieve(store, SearchRequest(q='Find worker', workspace='warehouse', t_range=(8000, 9000)))
    assert all(8000 <= m['t0_ms'] < 9000 for m in r.envelope.output['moments'])


def test_box_rows_skips_absent_chunks():
    # the pipeline writes no chunk file for a 10 s window without boxes (wh-ss-warehouse_017 camera_01 has no 00015.json)
    from app.api.search import box_rows
    class Store:
        def manifest(self, key):
            return {'assets': {'boxes_dir': 'boxes'}}
        def asset_json(self, key, path):
            if path != 'boxes/cam/00001.json':
                raise FileNotFoundError('Asset not found')
            return {'rows': [[12000, 'person'], [15000, 'person']]}
    assert box_rows(Store(), 'k', 'cam', 0, 30000) == [[12000, 'person'], [15000, 'person']]


@pytest.mark.parametrize('q', ['Did the person intend to cross?', 'The worker intended to dodge?',
    'The person intends to cross?', 'Was the person intending to cross?', 'Was crossing the intention?',
    'Did the person mean it, or was it meant to happen?', 'Did the person cross on purpose?',
    'Who drove the car past the person?', 'Who was driving when the pedestrian crossed?', 'Whose car crossed?'])
def test_b1_intent_and_identity_are_unsupported(monkeypatch, q):
    monkeypatch.setenv('LLM_URL', 'http://llm/v1')
    parsed = llm.rule_parse(q, {'workspace':'fleet'})
    assert parsed.template=='unsupported'
    assert not llm.needs_llm(q, parsed)


@pytest.mark.parametrize('time_text', ['at 5 s', 'at 5 seconds', 'at second 5', 'at 5.25 seconds'])
def test_b1_point_time_intersects_context_interval_without_widening(time_text):
    start = 5250 if '5.25' in time_text else 5000
    parsed = llm.rule_parse('How far was the worker from the forklift '+time_text+'?',
                            {'workspace':'warehouse','t_range':[0,10000]})
    assert parsed.t_range==(start,start+1)
    from app.core.investigation_context import apply_constraints
    constrained,reason=apply_constraints(
        'How far was the worker from the forklift '+time_text+'?',
        {'workspace':'warehouse','t_range':[0,10000]},parsed)
    assert constrained.t_range==(start,start+1) and reason is None


@pytest.mark.parametrize('context',[
    {'workspace':'warehouse','t_range':[6000,7000]},
    {'workspace':'warehouse','t_range':[0,10000],'t_ms':5001},
])
def test_point_time_never_overrides_conflicting_context(context):
    from app.core.investigation_context import apply_constraints
    question='How far was the worker from the forklift at second 5?'
    parsed=llm.rule_parse(question,context)
    constrained,reason=apply_constraints(question,context,parsed)
    assert reason=='conflicting_time_constraints'
    assert constrained.t_range[0]>=constrained.t_range[1]
