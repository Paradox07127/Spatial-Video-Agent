"""Follow-ups no template measures answer with an explanation, the measured facts at the focus and suggestions."""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.agent import TEXT
from app.core import followup, llm
from app.tests.unit.test_investigation_api import decode
from app.tests.unit.test_investigations import index  # noqa: F401
from pipeline.common.schema_check import check_sse_sequence

ROOT = Path(__file__).resolve().parents[3]
KEY = 'wh-sdg-fixture-01'
CTX = {'workspace':'warehouse','scene_key':KEY}
FOCUS = {**CTX,'t_ms':4900,'selection':['P1','F1']}
PRESET = 'How close did the forklift come to a person?'
ENGLISH_ONLY = 'Supported questions are in English'


@pytest.fixture
def client(index, tmp_path, monkeypatch):  # noqa: F811
    from app.main import create_app
    for key, value in {'MODE':'dev', 'DATA_BACKEND':'duckdb', 'INDEX_DUCKDB':str(index),
            'APP_DUCKDB':str(tmp_path/'app.duckdb'), 'RUNS_DIR':str(tmp_path/'runs'),
            'ASSET_DIR':str(ROOT/'fixtures/sva-out'), 'LLM_URL':'', 'VLM_URL':'',
            'WANDB_API_KEY':'', 'WEAVE_PROJECT':'', 'EMBED_URL':''}.items():
        monkeypatch.setenv(key, value)
    with TestClient(create_app()) as c:
        yield c


class Session:
    def __init__(self, client):
        self.client = client
        self.sid = client.post('/api/agent/sessions',json={'title':'Follow-up review'}).json()['session_id']
        self.revision = 0
        first = self.ask(PRESET,CTX)
        assert status(first)['final'] == 'SUPPORTED'

    def ask(self, q, context):
        body = dict(q=q,protocol_version=2,session_id=self.sid,request_id='r'+str(self.revision),
                    expected_revision=self.revision,context=context)
        if self.revision:
            body['parent_turn_id'] = self.parent
        response = self.client.post('/api/agent/ask',json=body)
        frames = decode(response)
        if not self.revision:
            self.parent = response.headers['X-SVA-Turn']
        self.revision += 1
        return frames


def status(frames):
    return next(f['data'] for f in frames if f['event']=='status')


def answers(frames, part=None):
    return [f['data'] for f in frames if f['event']=='answer' and (part is None or f['data'].get('part')==part)]


def evidence(frames):
    return {e['eid']:e for f in frames if f['event']=='tool_result' for e in f['data']['evidence']}


def ui(frames, cmd):
    return [f['data'] for f in frames if f['event']=='ui' and f['data']['cmd']==cmd]


def suggest(frames):
    return [f['data'] for f in frames if f['event']=='suggest']


def sentences(text):
    return [s for s in re.split(r'(?<=[.?!])\s+',text) if s]


def test_activity_follow_up_explains_measures_and_suggests(client):
    session = Session(client)
    frames = session.ask('What was the worker doing at that moment?',FOCUS)
    assert check_sse_sequence(frames,protocol_version=2) == []
    assert status(frames)['final'] == 'UNRESOLVED'
    unmet = answers(frames,'unmet')
    assert len(unmet) == 1
    assert 'What a person is doing is not measured' in unmet[0]['text']
    assert 'unconsumed' not in unmet[0]['text'] and 'Not measured:' not in unmet[0]['text']
    facts = answers(frames,'fact')
    assert len(facts) >= 2
    read = evidence(frames)
    distance = [f for f in facts if any(read[r['eid']]['method']=='ground_distance' for r in f['measure_refs'])]
    assert distance and 'P1' in distance[0]['text'] and 'F1' in distance[0]['text']
    assert any(e['method']=='ground_distance' for e in read.values())
    assert all('unconsumed' not in a['text'] and not a['text'].startswith('Not measured:') for a in answers(frames))
    assert ui(frames,'seek')[-1]['args']['t_ms'] == 4900
    assert ui(frames,'highlight')[-1]['args']['ids'] == ['P1','F1']
    assert any(a['args']['kind']=='label' for a in ui(frames,'annotate'))
    [proposed] = suggest(frames)
    assert 1 <= len(proposed['questions']) <= 3
    # d: every suggestion is answerable in the same session
    for q in proposed['questions']:
        answered = session.ask(q,CTX)
        assert check_sse_sequence(answered,protocol_version=2) == []
        assert status(answered)['final'] in {'SUPPORTED','CONTRADICTED'}, (q,answers(answered))


def test_camera_follow_up_names_the_detecting_cameras(client):
    session = Session(client)
    frames = session.ask('Which camera saw it best?',FOCUS)
    assert check_sse_sequence(frames,protocol_version=2) == []
    assert status(frames)['final'] == 'UNSUPPORTED'
    [unmet] = answers(frames,'unmet')
    assert 'no measured view-quality score' in unmet['text']
    visibility = next(e for e in evidence(frames).values() if e['method']=='floor_cell_visibility')
    observed = visibility['value']['person']['observed_in']
    assert observed
    assert any(all(cam in a['text'] for cam in observed) and 'detected P1' in a['text'] for a in answers(frames,'fact'))
    assert len(suggest(frames)) == 1


def test_non_english_follow_up_gets_the_english_note_and_facts(client):
    session = Session(client)
    frames = session.ask('那个时候工人在做什么？',FOCUS)
    assert check_sse_sequence(frames,protocol_version=2) == []
    [unmet] = answers(frames,'unmet')
    assert ENGLISH_ONLY in unmet['text']
    assert answers(frames,'fact')


def ask_v1(client, q):
    return decode(client.post('/api/agent/ask',json={'q':q,'context':CTX}))


def test_v1_passing_question_has_no_repeated_or_internal_text(client):
    frames = ask_v1(client,'What was the worker doing when the forklift passed?')
    assert check_sse_sequence(frames) == []
    texts = [a['text'] for a in answers(frames)]
    assert texts and all('unconsumed' not in t for t in texts)
    assert len(texts) == len(set(texts))
    assert all(len(sentences(t)) == len(set(sentences(t))) for t in texts)
    assert all('text_spans' not in a and 'model_refs' not in a for a in answers(frames))


def test_v1_gaze_question_explains_without_internal_tokens(client):
    frames = ask_v1(client,'Is the forklift driver looking at the worker?')
    assert check_sse_sequence(frames) == []
    texts = [a['text'] for a in answers(frames)]
    assert all('unconsumed' not in t for t in texts)
    assert any('Where a person is looking is not measured.' in t for t in texts)


def test_identity_question_keeps_the_refusal(client):
    frames = ask_v1(client,'Who is the person in eye_00?')
    assert check_sse_sequence(frames) == []
    assert status(frames)['final'] == 'UNSUPPORTED'
    assert [a['text'] for a in answers(frames)] == [TEXT['UNSUPPORTED']]
    assert not answers(frames,'unmet') and not suggest(frames)


def parsed(q, **ctx):
    return llm.rule_parse(q,{**CTX,**ctx})


def test_unmet_parts_are_plain_sentences():
    q = 'What was the worker doing at that moment?'
    parts = followup.unmet_parts(q,parsed(q),True)
    assert parts[0].startswith('What a person is doing is not measured')
    assert '“That moment” refers to the time and objects selected on the timeline.' in parts
    assert 'The earlier moment this question refers to could not be identified.' in followup.unmet_parts(q,parsed(q),False)
    q = 'Is the forklift driver looking at the worker?'
    assert followup.unmet_parts(q,parsed(q),False) == ['Where a person is looking is not measured.',
                                                       '“driver” is not something the tools measure.']
    q = 'Which camera saw it best?'
    parts = followup.unmet_parts(q,parsed(q),True)
    assert parts.count('There is no measured view-quality score; below are the cameras that detected the person.') == 1
    assert not any('looking' in p for p in parts)
    q = 'What was the worker doing when the forklift passed?'
    assert any('only dodge and near-miss events' in p for p in followup.unmet_parts(q,parsed(q),False))
    q = '那个时候工人在做什么？'
    assert any(ENGLISH_ONLY in p for p in followup.unmet_parts(q,parsed(q),True))
    for q in ['Which camera saw it best?','What is the worker carrying_box doing then?','Is the red worker busy?']:
        for p in followup.unmet_parts(q,parsed(q),False):
            assert 'unconsumed' not in p and not re.search(r'\w:\w|[a-z]+_[a-z]+',p)
            assert not re.search(r'“(?:which|what|who|how|when|where|is|was|did)”',p,re.I)


def test_suggestions_are_answerable_templates():
    focus = {'scene_key':KEY,'t_ms':4900,'person':'P1','forklift':'F1'}
    assert followup.suggestions('warehouse',focus) == [
        'How close did the forklift come to a person between 2 and 7 seconds?',
        'How fast was F1 at 4.9 seconds?',
        'Where was P1 at 4.9 seconds?']
    assert followup.suggestions('warehouse',{**focus,'forklift':None,'t_ms':1000}) == [
        'How close did the forklift come to a person between 0 and 3 seconds?','Where was P1 at 1.0 seconds?']
    assert followup.suggestions('fleet',focus) == [] and followup.suggestions('warehouse',None) == []
