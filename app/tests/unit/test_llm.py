import httpx
import pytest

from app.core import llm


@pytest.mark.parametrize('env,auth', [({'LLM_API_KEY': 'k', 'GPU_BEARER_TOKEN': 'g'}, 'Bearer k'),
                                      ({'GPU_BEARER_TOKEN': 'g'}, None)])
def test_complete_json_auth(monkeypatch, env, auth):
    monkeypatch.setenv('LLM_URL', 'http://llm/v1')
    monkeypatch.delenv('LLM_API_KEY', raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"ok": 1}'}}]})
    client = httpx.Client
    monkeypatch.setattr(llm.httpx, 'Client', lambda **kw: client(transport=httpx.MockTransport(handler), **kw))
    assert llm.complete_json([{'role': 'user', 'content': 'q'}], role='parse') == {'ok': 1}
    assert seen[0].headers.get('Authorization') == auth


ANCHOR = 'Show me every clip, from any camera in any site, where a person is close to a moving vehicle'


@pytest.mark.parametrize('ctx,q,scope', [
    ({'workspace': 'warehouse'}, ANCHOR, 'scene'),
    ({'workspace': 'warehouse', 'scope': 'corpus'}, ANCHOR, 'corpus'),
    ({'workspace': 'street', 'scope': 'corpus', 'scene_key': 'st-fixture_cam_1-clip01'}, ANCHOR, 'corpus'),
    ({'workspace': 'warehouse', 'scope': 'corpus'}, 'Where is P2?', 'scene'),
    ({'workspace': 'warehouse', 'scope': 'corpus'}, 'Link f1 across cameras', 'scene'),
    ({'workspace': 'warehouse', 'scope': 'corpus', 't_ms': 0}, ANCHOR, 'scene'),
    ({'workspace': 'warehouse', 'scope': 'corpus', 'selection': ['P1']}, ANCHOR, 'scene'),
    ({'workspace': 'warehouse', 'scope': 'corpus', 'selection': []}, ANCHOR, 'corpus'),
    ({'workspace': 'warehouse', 'scope': 'scene'}, ANCHOR, 'scene'),
])
def test_scope_rule(ctx, q, scope):
    assert llm.rule_parse(q, ctx).scope == scope


@pytest.mark.parametrize('ctx,q,scope,sources', [
    ({'workspace': 'warehouse', 'sources': ['fixed']}, ANCHOR, 'corpus', ['fixed']),
    ({'workspace': 'warehouse', 'scope': 'scene', 'sources': ['warehouse', 'fleet']}, ANCHOR, 'corpus', ['warehouse', 'fleet']),
    ({'workspace': 'warehouse', 'sources': []}, ANCHOR, 'scene', None),
    ({'workspace': 'warehouse', 'sources': ['warehouse']}, 'Where is P2?', 'scene', None),
    ({'workspace': 'warehouse', 'sources': ['warehouse'], 't_ms': 0}, ANCHOR, 'scene', None),
    ({'workspace': 'warehouse', 'sources': ['warehouse'], 'selection': ['P1']}, ANCHOR, 'scene', None),
])
def test_sources_make_the_ask_corpus_unless_it_is_local_to_one_scene(ctx, q, scope, sources):
    parsed = llm.rule_parse(q, ctx)
    assert (parsed.scope, parsed.sources) == (scope, sources)


def test_scope_passes_through_the_llm_parse(monkeypatch):
    monkeypatch.setenv('LLM_URL', 'http://llm/v1')
    monkeypatch.setattr(llm, 'complete_json', lambda *a, **k: {'template': 'find', 'entities': ['person']})
    q = llm.parse('Anything odd on the street?', {'workspace': 'street', 'scope': 'corpus'})
    assert (q.template, q.workspace, q.scope) == ('find', 'street', 'corpus')


@pytest.mark.parametrize('q', [ANCHOR, 'Is the worker next to the forklift?', 'Was a person close to a car?'])
def test_close_to_and_next_to_are_distance_words_not_places(q):
    parsed = llm.rule_parse(q, {'workspace': 'warehouse'})
    assert (parsed.template, parsed.place) == ('find', None)


@pytest.mark.parametrize('q,place', [('Find workers near a forklift', None), ('Is anyone near the moving car?', None),
                                     ('near Union Station, any brake?', 'Union Station'), ('Near ROM, did any pedestrian cross?', 'ROM')])
def test_only_a_capitalised_name_after_near_is_a_place(q, place):
    assert llm.rule_parse(q, {'workspace': 'fleet'}).place == place
