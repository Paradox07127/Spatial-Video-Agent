"""The real application routes persist model answers across HTTP and process lifetimes."""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.tests.unit.test_investigations import index
from pipeline.common.schema_check import check_sse_sequence


ROOT = Path(__file__).resolve().parents[3]
KEY = 'wh-sdg-fixture-01'


def decode(response):
    assert response.status_code == 200, response.text
    result = []
    for block in response.text.split('\n\n'):
        fields = dict(line.split(': ', 1) for line in block.splitlines() if ': ' in line)
        if 'event' in fields:
            result.append({'event': fields['event'], 'data': json.loads(fields['data'])})
    return result


def test_application_durable_model_answers_inherit_scope_replay_and_restart(index, tmp_path, monkeypatch):
    from app.core import vlm
    from app.main import create_app

    for key, value in {'MODE':'dev', 'DATA_BACKEND':'duckdb', 'INDEX_DUCKDB':str(index),
            'APP_DUCKDB':str(tmp_path/'app.duckdb'), 'RUNS_DIR':str(tmp_path/'runs'),
            'ASSET_DIR':str(ROOT/'fixtures/sva-out'), 'LLM_URL':'', 'VLM_URL':'',
            'WANDB_API_KEY':'', 'WEAVE_PROJECT':'', 'EMBED_URL':''}.items():
        monkeypatch.setenv(key, value)
    inputs = []

    def observe(scene_key, cam, t_range, actors, **kwargs):
        inputs.append((scene_key, cam, list(t_range), [a.entity_id for a in actors]))
        lo, hi = t_range
        refs = [{'cam_id':cam, 't_ms':lo}]
        return dict(cam_id=cam,t0_ms=lo,t1_ms=hi,input_frame_refs=refs,
            facts=[dict(subject_ids=[],text='A vehicle is visible beside a pedestrian.',
                        frame_refs=refs,uncertainty=.3)],
            model_rev='api-fixture-model',prompt_rev='observation-1',
            input_digest='sha256:'+'a'*64,exec_state='live')

    monkeypatch.setattr(vlm, 'observe_window', observe)
    with TestClient(create_app()) as client:
        session = client.post('/api/agent/sessions',json={'title':'Bounded source review'})
        assert session.status_code == 201
        sid = session.json()['session_id']
        initial = dict(q='Describe the scene',protocol_version=2,session_id=sid,
            request_id='api-first',expected_revision=0,
            context={'workspace':'warehouse','scene_key':KEY,'cam_id':'eye_00',
                     't_range':[1000,3000],'t_ms':2000,'selection':['P1']})
        response = client.post('/api/agent/ask',json=initial)
        first = decode(response)
        assert check_sse_sequence(first,protocol_version=2) == []
        assert 'id: 1\n' in response.text
        assert inputs == [(KEY,'eye_00',[2000,2001],['P1'])]
        assert response.headers['X-SVA-Session'] == sid
        jid, tid = response.headers['X-SVA-Job'], response.headers['X-SVA-Turn']
        observation = next(f['data']['observation'] for f in first if f['event']=='observation')
        answer = next(f['data'] for f in first if f['event']=='answer')
        assert observation['verification'] == 'unverified'
        assert answer['model_refs'][0]['eid'] == observation['eid'] and answer['measure_refs'] == []
        assert next(f['data']['final'] for f in first if f['event']=='status') == 'UNRESOLVED'
        duplicate = client.post('/api/agent/ask',json=initial)
        assert duplicate.headers['X-SVA-Job'] == jid and len(inputs) == 1
        assert decode(duplicate) == first
        history = client.get('/api/agent/sessions/'+sid).json()
        assert history['revision'] == 1 and len(history['turns'][0]['observations']) == 1
        assert history['turns'][0]['answers'][0]['text'] == answer['text']
        followup = dict(q='Describe it again',protocol_version=2,session_id=sid,
            parent_turn_id=tid,request_id='api-second',expected_revision=1,context={})
        second_response = client.post('/api/agent/ask',json=followup)
        second = decode(second_response)
        assert check_sse_sequence(second,protocol_version=2) == []
        assert inputs[-1] == inputs[0]
        changed_source = dict(q='Describe the scene',protocol_version=2,session_id=sid,
            parent_turn_id=second_response.headers['X-SVA-Turn'],request_id='api-third',
            expected_revision=2,context={'workspace':'fleet','scene_key':'fl-pie-fixture-01'})
        third_response = client.post('/api/agent/ask',json=changed_source)
        third = decode(third_response)
        assert check_sse_sequence(third,protocol_version=2) == []
        assert inputs[-1][0] == 'fl-pie-fixture-01' and inputs[-1][2:] == ([0,6000],[])
        third_jid = third_response.headers['X-SVA-Job']
        assert client.get('/api/agent/jobs/'+third_jid).json()['status'] == 'done'
        # The same mounted route still emits the unchanged v1 measurement contract.
        legacy = decode(client.post('/api/agent/ask',json={'q':'Where is P2?',
            'context':{'workspace':'warehouse','scene_key':KEY,'t_ms':2000}}))
        assert check_sse_sequence(legacy) == []
        assert not any(f['event']=='observation' for f in legacy)
        assert all('text_spans' not in f['data'] for f in legacy if f['event']=='answer')
        assert client.post('/api/agent/ask',json={**initial,'request_id':'stale-new'}).status_code == 409
        assert client.post('/api/agent/ask',json={'q':'Describe the scene','protocol_version':2,
            'request_id':'orphan','context':{'workspace':'warehouse'}}).status_code == 422

    call_count = len(inputs)
    with TestClient(create_app()) as client:
        restored = client.get('/api/agent/sessions/'+sid).json()
        assert restored['revision'] == 3 and len(restored['turns']) == 3
        assert all(t['status']=='done' and len(t['observations']) == 1 for t in restored['turns'])
        replay = decode(client.get('/api/agent/jobs/'+third_jid+'/events?after_seq=0'))
        assert replay == third
        assert len(inputs) == call_count
