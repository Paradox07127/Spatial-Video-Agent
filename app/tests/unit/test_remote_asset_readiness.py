import copy
import io
import json
from pathlib import Path
from threading import Event, Lock
import time
from types import SimpleNamespace

import pytest

from app.api.health import health
from app.core import asset_readiness as readiness


SCENE = {'scene_key':'st-example','analysis_rev':'r1','workspace':'street',
         'assets':{'dets_dir':'dets'},
         'cams':[{'id':'cam','video':None,'thumbs':'thumbs/cam',
                  'chunks':[{'uri':'s3://chunks/clip.mp4'}]}]}


def settings(tmp_path):
    return SimpleNamespace(s3_endpoint='https://private-object-store',asset_bucket='assets',
                           mode='site',lib_dir=tmp_path)


class FakeS3:
    def __init__(self,scene=SCENE):
        self.scene = scene
        self.calls = []
        self.closed = False
        self.bodies = []
        self.missing = False
        self.lock = Lock()
        self.active = self.maximum = 0
        self.gate = None
    def enter(self,kind,params):
        with self.lock:
            self.calls.append((kind,params))
            self.active+=1
            self.maximum=max(self.maximum,self.active)
        if self.gate:
            self.gate.wait(2)
        with self.lock:
            self.active-=1
        if self.missing:
            raise RuntimeError('SECRET_KEY and private endpoint must not be exposed')
    def get_object(self,**params):
        self.enter('get',params)
        body=io.BytesIO(json.dumps(self.scene).encode())
        self.bodies.append(body)
        return {'Body':body}
    def head_object(self,**params):
        self.enter('head',params)
        return {'ContentLength':100}
    def list_objects_v2(self,**params):
        self.enter('list',params)
        return {'Contents':[{'Key':params['Prefix']+'00000.jpg','Size':12}]}
    def close(self): self.closed=True


def finish(check):
    check.worker.join(3)
    assert not check.worker.is_alive()


def test_remote_validation_is_async_cached_and_checks_manifest_and_chunk_bucket(tmp_path,monkeypatch):
    client=FakeS3()
    gate=Event()
    client.gate=gate
    monkeypatch.setattr(readiness,'readiness_client',lambda cfg:client)
    check=readiness.RemoteAssetReadiness()
    cfg=settings(tmp_path)
    assert check.status(cfg,[SCENE])['state']=='validating'
    assert check.status(cfg,[SCENE])['state']=='validating'
    gate.set()
    finish(check)
    result=check.status(cfg,[SCENE])
    assert result['state']=='ready' and result['checked']==result['total']==3
    assert client.maximum<=readiness.CONCURRENCY and all(body.closed for body in client.bodies)
    assert ('head',{'Bucket':'chunks','Key':'clip.mp4'}) in client.calls
    assert ('get',{'Bucket':'assets','Key':'sva/st-example/manifest.json'}) in client.calls
    assert any(kind=='list' and params['Prefix']=='sva/st-example/thumbs/cam/' for kind,params in client.calls)
    assert client.closed
    calls=len(client.calls)
    assert check.status(cfg,[SCENE])['state']=='ready' and len(client.calls)==calls


def test_configuration_does_not_prove_remote_assets_exist(tmp_path,monkeypatch):
    client=FakeS3()
    client.missing=True
    monkeypatch.setattr(readiness,'readiness_client',lambda cfg:client)
    check=readiness.RemoteAssetReadiness()
    assert check.status(settings(tmp_path),[SCENE])['state']=='validating'
    finish(check)
    out=check.status(settings(tmp_path),[SCENE])
    assert out['state']=='failed' and out['reason']=='remote_asset_unavailable'
    assert 'SECRET' not in str(out) and 'private' not in str(out)


def test_indexed_and_remote_manifest_must_match_and_changed_release_rechecks(tmp_path,monkeypatch):
    client=FakeS3()
    monkeypatch.setattr(readiness,'readiness_client',lambda cfg:client)
    check=readiness.RemoteAssetReadiness()
    cfg=settings(tmp_path)
    check.status(cfg,[SCENE])
    finish(check)
    assert check.status(cfg,[SCENE])['state']=='ready'
    newer={**copy.deepcopy(SCENE),'analysis_rev':'r2'}
    assert check.status(cfg,[newer])['state']=='validating'
    finish(check)
    assert check.status(cfg,[newer])['reason']=='remote_manifest_mismatch'


def test_prefixes_and_fleet_map_references_are_explicit():
    scene={'scene_key':'fl-example','workspace':'fleet','assets':{'ego_dir':'ego','events':'events.json'},
           'cams':[{'id':'dashcam','video':'video/dashcam.mp4','thumbs':None}]}
    refs=readiness.references([scene],'assets')
    assert ('prefix','assets','sva/fl-example/ego/',None) in refs
    assert ('file','assets','sva/fl-example/video/dashcam.mp4',None) in refs
    assert {r[2] for r in refs if r[2].startswith('sva/_map/')}=={
        'sva/_map/style.json','sva/_map/basemap.pmtiles','sva/_map/buildings.pmtiles'}


def test_empty_prefix_is_not_ready_and_oversized_manifest_is_rejected(tmp_path,monkeypatch):
    client=FakeS3()
    client.list_objects_v2=lambda **kw:{'Contents':[]}
    monkeypatch.setattr(readiness,'readiness_client',lambda cfg:client)
    check=readiness.RemoteAssetReadiness()
    check.status(settings(tmp_path),[SCENE]);finish(check)
    assert check.status(settings(tmp_path),[SCENE])['reason']=='remote_asset_prefix_empty'
    client.list_objects_v2=lambda **kw:{'Contents':[{'Size':10}]}
    monkeypatch.setattr(readiness,'MANIFEST_LIMIT',1)
    check=readiness.RemoteAssetReadiness()
    check.status(settings(tmp_path),[SCENE]);finish(check)
    assert check.status(settings(tmp_path),[SCENE])['reason']=='remote_manifest_too_large'


def test_validation_timeout_stops_queue_and_bounds_concurrency(tmp_path,monkeypatch):
    client=FakeS3()
    client.gate=Event()
    monkeypatch.setattr(readiness,'readiness_client',lambda cfg:client)
    monkeypatch.setattr(readiness,'CHECK_SECONDS',.03)
    scene={**copy.deepcopy(SCENE),'assets':{f'file{i}':f'file{i}.json' for i in range(30)}}
    check=readiness.RemoteAssetReadiness()
    check.status(settings(tmp_path),[scene])
    time.sleep(.06)
    assert check.status(settings(tmp_path),[scene])['reason']=='remote_asset_validation_timeout'
    assert len(client.calls)<=readiness.CONCURRENCY
    client.gate.set();finish(check)
    assert client.maximum<=readiness.CONCURRENCY


def request(tmp_path,store,check=None):
    # These tests isolate asset validation; the independent app connection is healthy.
    store.lock = Lock()
    store.app = SimpleNamespace(execute=lambda *a: SimpleNamespace(fetchone=lambda: None))
    state=SimpleNamespace(store=store,settings=settings(tmp_path),vendor_manifest={'files':[]})
    if check:
        state.remote_asset_readiness=check
    return SimpleNamespace(app=SimpleNamespace(state=state))


def test_health_waits_for_real_remote_validation_and_preserves_local_contract(tmp_path,monkeypatch):
    client=FakeS3()
    monkeypatch.setattr(readiness,'readiness_client',lambda cfg:client)
    store=SimpleNamespace(asset_dir=None,backend='duckdb',list_scenes=lambda:[SCENE],index_counts=lambda:{'scenes':1})
    req=request(tmp_path,store)
    response=health(req)
    assert response.status_code==503 and not json.loads(response.body)['assets_ok']
    finish(req.app.state.remote_asset_readiness)
    response=health(req)
    assert response.status_code==200 and json.loads(response.body)['assets_ok']
    assert json.loads(response.body)['asset_validation']['state']=='ready'
    local=SimpleNamespace(asset_dir=tmp_path,backend='duckdb',list_scenes=lambda:[],index_counts=lambda:{})
    response=health(request(tmp_path,local))
    assert response.status_code==503
    assert set(json.loads(response.body))=={'ok','mode','backend','libs_ready','index_rows','assets_ok','agent_protocols'}
    assert json.loads(response.body)['agent_protocols']==[1,2]


def test_s3_validation_client_has_bounded_timeouts_and_missing_credentials_fail_closed(monkeypatch,tmp_path):
    import boto3
    seen=[]
    monkeypatch.setattr(boto3,'client',lambda *a,**kw:seen.append(kw) or FakeS3())
    cfg=settings(tmp_path)
    cfg.access_key,cfg.secret_key,cfg.s3_verify='fixture','fixture',True
    readiness.readiness_client(cfg)
    config=seen[0]['config']
    assert config.connect_timeout==1 and config.read_timeout==2
    assert config.max_pool_connections==readiness.CONCURRENCY
    assert config.retries['max_attempts']==0
    cfg.secret_key=''
    with pytest.raises(readiness.ValidationFailure,match='credentials_missing'):
        readiness.readiness_client(cfg)
    assert len(seen)==1


def test_prefix_validation_handles_empty_folder_markers_before_real_files():
    responses=iter([{'Contents':[{'Size':0}],'IsTruncated':True,'NextContinuationToken':'next'},
                    {'Contents':[{'Size':1}]}])
    requests=[]
    def listing(**params):
        requests.append(params)
        return next(responses)
    readiness.validate_reference(SimpleNamespace(list_objects_v2=listing),
                                  ('prefix','assets','sva/scene/boxes/',None),time.monotonic()+1)
    assert requests[1]['ContinuationToken']=='next' and requests[1]['MaxKeys']==20
