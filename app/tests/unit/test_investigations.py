import json
from pathlib import Path
import subprocess
import sys
from threading import Event
import time

from fastapi import HTTPException
import pytest

from app.api.agent import AskRequest
from app.core.investigations import Investigations
from app.core.store import Store

ROOT = Path(__file__).resolve().parents[3]
KEY = 'wh-sdg-fixture-01'


@pytest.fixture(scope='module')
def index(tmp_path_factory):
    root = tmp_path_factory.mktemp('investigation-index')
    db = root / 'index.duckdb'
    subprocess.run([sys.executable, str(ROOT/'fixtures/gen_fixture.py'), '--no-media', '--out', str(root), '--index', str(db)], check=True, capture_output=True)
    return db


@pytest.fixture
def store(index, tmp_path, monkeypatch):
    monkeypatch.setenv('RUNS_DIR', str(tmp_path/'runs'))
    s = Store(index_path=index, app_path=tmp_path/'app.duckdb', asset_dir=ROOT/'fixtures/sva-out')
    yield s
    s.close()


def body(session, **kw):
    return AskRequest.model_validate(dict(q='Describe this scene', protocol_version=2,
        session_id=session['session_id'], expected_revision=session['revision'], request_id='request-1',
        context={'workspace':'warehouse','scene_key':KEY,'t_range':[0,1000]}, **kw))


def frames(store, body, **kw):
    rid = 'r_0123456789abcdef'
    yield {'event':'step', 'data':{'run_id':rid,'state':'parse','i':0}}
    yield {'event':'answer','data':{'run_id':rid,'text':'A visible vehicle moves.'}}
    yield {'event':'status','data':{'run_id':rid,'final':'UNRESOLVED'}}
    yield {'event':'done','data':{'run_id':rid}}


def await_done(service, jid):
    end = time.monotonic()+5
    while time.monotonic()<end:
        row = service.job(jid)
        if row['status'] in {'done','cancelled','failed'}:
            return row
        time.sleep(.01)
    raise AssertionError(service.job(jid))


def test_idempotency_revision_parent_and_history(store):
    s = Investigations(store, runner=frames)
    session = s.create('Source review')
    first = body(session)
    job = s.submit(first)
    assert s.submit(first)['job_id'] == job['job_id']
    with pytest.raises(HTTPException) as conflict:
        s.submit(first.model_copy(update={'q':'A different request'}))
    assert conflict.value.status_code == 409
    with pytest.raises(HTTPException) as conflict:
        s.submit(first.model_copy(update={'request_id':'request-2'}))
    assert conflict.value.status_code == 409
    s.start()
    try:
        assert await_done(s,job['job_id'])['status']=='done'
        history = s.session(session['session_id'])
        assert history['revision']==1 and history['turns'][0]['answers'][0]['text']=='A visible vehicle moves.'
        second = AskRequest.model_validate(dict(q='Describe it again',protocol_version=2,
          session_id=session['session_id'],parent_turn_id=job['turn_id'],expected_revision=1,
          request_id='request-2',context={}))
        nextjob=s.submit(second)
        assert s.one('investigation_turns','turn_id',nextjob['turn_id'])['context']['t_range']==[0,1000]
        assert await_done(s,nextjob['job_id'])['status']=='done'
    finally:s.close()


def test_disconnection_is_only_subscription_and_replay_is_ordered(store):
    s=Investigations(store,runner=frames);session=s.create();job=s.submit(body(session));s.start()
    try:
        subscriber=s.stream(job['job_id'])
        first=next(subscriber)
        while first.startswith(':'):
            first=next(subscriber)
        subscriber.close()
        assert 'id: 1\n' in first
        assert await_done(s,job['job_id'])['status']=='done'
        replay=list(s.stream(job['job_id'],after_seq=1))
        assert len(replay)==3 and replay[0].startswith('id: 2\n') and 'event: done' in replay[-1]
    finally:s.close()


def test_cancel_blocks_late_answer_and_observation(store):
    reached,release=Event(),Event()
    def blocking(store,body,**kw):
        yield next(frames(store,body))
        reached.set();assert release.wait(4)
        yield {'event':'observation','data':{'run_id':'r_0123456789abcdef','observation':{}}}
        yield list(frames(store,body))[1]
    s=Investigations(store,runner=blocking);session=s.create();job=s.submit(body(session));s.start()
    try:
        assert reached.wait(4)
        assert s.cancel(job['job_id'])['cancel_requested']
        release.set()
        assert await_done(s,job['job_id'])['status']=='cancelled'
        assert s.session(session['session_id'])['turns'][0]['observations']==[]
        assert s.session(session['session_id'])['turns'][0]['answers']==[]
        assert s.job(job['job_id'])['last_seq']==1
    finally:release.set();s.close()


def test_recovery_charges_budget_and_uses_new_attempt_without_rerunning_done(store):
    s=Investigations(store,runner=frames);session=s.create();job=s.submit(body(session))
    with s.transaction() as db:
        db.execute("UPDATE investigation_jobs SET status='running',attempt=1,spent_ms=40000,lease_started=current_timestamp WHERE job_id=?",[job['job_id']])
    s.start()
    try:
        result=await_done(s,job['job_id'])
        assert result['status']=='failed' and 'budget exhausted' in result['error']
    finally:s.close()
    # A fresh instance retains failed history and never schedules it again.
    restored=Investigations(store,runner=lambda *a,**kw:pytest.fail('terminal task reran'))
    restored.start()
    try:assert restored.job(job['job_id'])['attempt']==1
    finally:restored.close()


def test_pending_turn_survives_database_reopen(store):
    s=Investigations(store,runner=frames);session=s.create();job=s.submit(body(session))
    path,index,assets=store.app_path,store.index_path,store.asset_dir
    store.close()
    reopened=Store(index_path=index,app_path=path,asset_dir=assets)
    restored=Investigations(reopened,runner=frames);restored.start()
    try:
        assert await_done(restored,job['job_id'])['status']=='done'
        assert len(restored.session(session['session_id'])['turns'])==1
    finally:restored.close();reopened.close()


def test_source_revision_does_not_silently_reuse_new_media(store):
    s=Investigations(store,runner=frames);session=s.create()
    s.release_id='another-source-release'
    with pytest.raises(HTTPException) as conflict:s.submit(body(session))
    assert conflict.value.status_code==409


def test_visual_observation_is_typed_frame_evidence_and_never_measurement(store,monkeypatch):
    from app.core.model_observation import observe
    from app.core.models import ModelObservation
    receipt=dict(cam_id='ceiling_00',t0_ms=0,t1_ms=1000,
        input_frame_refs=[{'cam_id':'ceiling_00','t_ms':0}],
        facts=[dict(subject_ids=[],text='A vehicle moves across the visible floor.',
                    frame_refs=[{'cam_id':'ceiling_00','t_ms':0}],uncertainty=.2)],
        model_rev='fixture-model',prompt_rev='observation-1',input_digest='sha256:'+'a'*64,exec_state='live')
    monkeypatch.setattr('app.core.vlm.observe_window',lambda *a,**kw:receipt)
    r=observe(store,KEY,'ceiling_00',[0,1000],[])
    observation=ModelObservation.model_validate(r.envelope.output['observation'])
    assert observation.verification=='unverified' and r.evidence[0].kind=='model'
    assert observation.eid==r.evidence[0].eid
    receipt['facts'][0]['text']='Two people move at 2 m/s.'
    invalid=observe(store,KEY,'ceiling_00',[0,1000],[])
    assert invalid.envelope.exec_state=='failed' and invalid.evidence==[]
    receipt['facts'][0]['text']='The vehicle moves.'
    receipt['facts'][0]['frame_refs'][0]['t_ms']=1100
    invalid=observe(store,KEY,'ceiling_00',[0,1000],[])
    assert invalid.envelope.exec_state=='failed' and invalid.evidence==[]


def test_source_change_does_not_inherit_stale_actor_or_camera(store):
    s=Investigations(store,runner=frames);session=s.create();first=body(session)
    first.context.selection=['P1'];job=s.submit(first)
    s.execute(s.one('investigation_jobs','job_id',job['job_id']))
    second=AskRequest.model_validate(dict(q='Describe this trip',protocol_version=2,
      session_id=session['session_id'],parent_turn_id=job['turn_id'],expected_revision=1,
      request_id='request-2',context={'workspace':'fleet','scene_key':'fl-pie-fixture-01'}))
    nextjob=s.submit(second)
    context=s.one('investigation_turns','turn_id',nextjob['turn_id'])['context']
    assert context['selection'] is None and context['t_range'] is None


def test_clean_backup_restores_sessions_jobs_and_events(store,tmp_path):
    from app.scripts.state_backup import backup, restore
    s=Investigations(store,runner=frames);session=s.create();job=s.submit(body(session))
    s.execute(s.one('investigation_jobs','job_id',job['job_id']))
    path,index,assets,runs=store.app_path,store.index_path,store.asset_dir,store.runs_dir
    store.close()
    # CLI functions accept Paths and require a closed writer.
    archive=tmp_path/'backup'
    backup(path,runs,archive)
    target=tmp_path/'restored.duckdb';targetruns=tmp_path/'restored-runs'
    restore(archive,target,targetruns)
    restored=Store(index_path=index,app_path=target,asset_dir=assets)
    try:
        history=Investigations(restored).session(session['session_id'])
        assert history['turns'][0]['status']=='done'
        assert history['turns'][0]['answers'][0]['text']=='A visible vehicle moves.'
    finally:restored.close()
