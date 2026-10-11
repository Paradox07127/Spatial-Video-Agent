"""Local authority, complete recovery and TLS configuration regressions."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import pyarrow as pa

from app.core import trace
from app.core.store import Store,APP_KEYS,now
from app.scripts.state_backup import backup,restore
from app.tests.unit.test_store import store,index_db
from app.settings import Settings


def test_site_blob_failure_keeps_completed_step_and_durable_retry(store,monkeypatch):
    store.settings.mode='site';store.settings.asset_bucket='bucket'
    calls=[]
    def fail(**kw):
        calls.append(store.lock._is_owned())
        raise OSError('offline')
    monkeypatch.setattr(store,'s3',lambda:SimpleNamespace(put_object=fail))
    run=trace.start_run(store,'q',{})
    with trace.traced(run,'inspect',{'large':'x'*9000},state='inspect') as step:
        step.output({'large':'y'*9000})
    trace.finish_run(run,'done',final='NO_MATCH',claims=[])
    result=store.get_run(run.run_id)
    assert result.status=='done' and result.steps[0].exec_state=='live'
    assert result.steps[0].output=={'large':'y'*9000}
    assert calls==[]  # upload is not in the step commit path
    assert store.retry_blobs()==0 and calls and not any(calls)
    assert store.rows(store.app,'SELECT attempts FROM blob_outbox')[0]['attempts']==1
    uploaded=[]
    monkeypatch.setattr(store,'s3',lambda:SimpleNamespace(put_object=lambda **kw:uploaded.append((kw,store.lock._is_owned()))))
    store.app.execute('UPDATE blob_outbox SET next_retry=0')
    pending=store.rows(store.app,'SELECT count(*) n FROM blob_outbox')[0]['n']
    assert store.retry_blobs()==pending and uploaded and not any(lock for _,lock in uploaded)
    assert store.rows(store.app,'SELECT count(*) n FROM blob_outbox')[0]['n']==0


def test_action_delivery_restore_keeps_composite_channels_and_sent_wins(store):
    rows=[{'action_id':'a_1','channel':'webhook','status':'failed','updated':now()},
          {'action_id':'a_1','channel':'wandb_alert','status':'failed','updated':now()},
          {'action_id':'a_1','channel':'wandb_alert','status':'sent','updated':now()}]
    store._restore_app_rows('action_deliveries',pa.Table.from_pylist(rows))
    assert [(r['channel'],r['status']) for r in store.rows(store.app,'SELECT * FROM action_deliveries ORDER BY channel')]==[('wandb_alert','sent'),('webhook','failed')]
    store.backend='vastdb'
    observed=[]
    store.write_vastdb=lambda tables:observed.append((tables['action_deliveries'].to_pylist(),store.lock._is_owned()))
    assert store._mirror('action_deliveries',('a_1','wandb_alert'))
    assert observed[0][0][0]['status']=='sent' and observed[0][1] is False
    assert store.rows(store.app,'SELECT count(*) n FROM mirror_outbox')[0]['n']==0


def test_clean_restore_keeps_pending_partial_and_blob(store,tmp_path,monkeypatch):
    store.settings.mode='site';store.settings.asset_bucket='bucket'
    run=trace.start_run(store,'pending',{})
    with trace.traced(run,'inspect',{},state='inspect') as step:step.output('x'*9000)
    store.app.execute('INSERT INTO action_deliveries VALUES (?,?,?,?)',['a_1','wandb_alert','sent',now()])
    store.app.execute('INSERT INTO action_deliveries VALUES (?,?,?,?)',['a_1','webhook','failed',now()])
    store.app.execute('INSERT INTO mirror_outbox VALUES (?,?,?)',['agent_runs',run.run_id,'generation'])
    app,runs,index,assets=store.app_path,store.runs_dir,store.index_path,store.asset_dir
    store.close()
    snapshot=tmp_path/'snapshot'
    saved=backup(app,runs,snapshot)
    assert saved['tables']['action_deliveries']==2
    target=tmp_path/'clean/app.duckdb';newruns=tmp_path/'clean/runs'
    restore(snapshot,target,newruns)
    monkeypatch.setenv('RUNS_DIR',str(newruns))
    other=Store(index_path=index,app_path=target,asset_dir=assets)
    try:
        restored=other.get_run(run.run_id)
        assert restored.status=='running' and restored.steps[0].output=='x'*9000
        assert other.rows(other.app,'SELECT count(*) n FROM action_deliveries')[0]['n']==2
        assert other.rows(other.app,'SELECT count(*) n FROM mirror_outbox')[0]['n']==1
        assert Path(other.rows(other.app,'SELECT local_path FROM blob_outbox')[0]['local_path']).is_relative_to(newruns)
    finally:other.close()
    with pytest.raises(ValueError,match='clean'):restore(snapshot,target,newruns)
    (snapshot/'runs'/run.run_id/next((snapshot/'runs'/run.run_id).iterdir()).name).write_text('tampered')
    with pytest.raises(ValueError,match='checksum'):restore(snapshot,tmp_path/'bad/app.duckdb',tmp_path/'bad/runs')
    assert not (tmp_path/'bad/app.duckdb').exists()


def test_asset_identity_never_persists_expiring_url(store,monkeypatch):
    store.asset_dir=None
    monkeypatch.setattr(store,'s3',lambda:SimpleNamespace(generate_presigned_url=lambda *a,**kw:'https://signed?expires=soon'))
    assert store.asset_url('wh-example','video/eye.mp4')=='/api/assets/wh-example/video/eye.mp4'
    assert store.presigned_asset_url('wh-example','video/eye.mp4')=='https://signed?expires=soon'


def test_tls_defaults_trusted_ca_and_resolved_model_alias(monkeypatch,tmp_path):
    monkeypatch.delenv('S3_VERIFY',raising=False);monkeypatch.delenv('VDB_VERIFY',raising=False)
    assert Settings().s3_verify is True and Settings().vdb_verify is True
    ca=tmp_path/'ca.pem';ca.write_text('test trusted CA path')
    monkeypatch.setenv('S3_VERIFY',str(ca))
    assert Settings().s3_verify==str(ca) and Settings().vdb_verify==str(ca)
    monkeypatch.setenv('S3_VERIFY',str(ca)+'missing')
    with pytest.raises(ValueError):Settings()


def test_restored_successful_channel_is_not_sent_again(store,monkeypatch):
    from app.api.actions import deliver_notifications
    from app.core.models import Action
    import sys
    store.settings.mode='site';store.settings.wandb_api_key='mock';store.settings.wandb_project='mock'
    store._restore_app_rows('action_deliveries',pa.Table.from_pylist([
        {'action_id':'a_1','channel':'wandb_alert','status':'sent','updated':now()},
        {'action_id':'a_1','channel':'webhook','status':'failed','updated':now()}]))
    attempts=[]
    monkeypatch.setitem(sys.modules,'wandb',SimpleNamespace(init=lambda **kw:None,alert=lambda **kw:attempts.append('wandb')))
    action=Action(action_id='a_1',scene_key='wh-sdg-fixture-01',event_id=None,run_id=None,claim_id=None,auto=False,
                  actions=['wandb_alert','webhook'],review_task_id=None,zone_id=None,delivered=False,created=now())
    store._insert_model('actions',action)
    assert deliver_notifications(store,action) is False
    assert attempts==[]


def test_legacy_signed_assets_hydrate_to_stable_identity(store):
    store.settings.s3_endpoint='https://storage.example';store.settings.asset_bucket='bucket'
    value={'url':'https://storage.example/bucket/sva/wh-example/video/eye.mp4?X-Amz-Signature=expired'}
    assert store._stable_assets(value)=={'url':'/api/assets/wh-example/video/eye.mp4'}
    assert store._stable_assets('https://other.example/bucket/sva/a')=='https://other.example/bucket/sva/a'
