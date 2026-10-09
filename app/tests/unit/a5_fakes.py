"""Fixture Store signatures and in-memory local tracing for the unmerged A1/P-X cards."""
from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

import duckdb

from app.core import geometry, trace, vlm
from app.core.models import AgentRun, CamFrame, Clip, CosmosRecord, Entity, Event, ForkliftAtMoment, Moment, PersonAtMoment, RunStep, TriggerResult
from app.core.store import Store
from fixtures.gen_fixture import build_index, pseudo_vec

FIXTURES = Path(__file__).resolve().parents[3] / 'fixtures'
L1_KEY, L1_CAM = 'st-fixture_cam_1-clip01', 'fixture_cam-1'
L1_SITE_CHUNK = 's3://team-38-vss-chunks/team-38/20261009_000000_clip01_chunk_0000.mp4'


class FixtureStore(Store):
    def __init__(self, *, index_path, app_path, asset_dir, backend='duckdb', allow_duckdb_fallback=False):
        self.index_path, self.asset_dir, self.backend = index_path,asset_dir,backend
        self.index, self.lock = duckdb.connect(str(index_path),read_only=True), RLock()
        self.runs_dir = Path(index_path).parent/'runs'
        self.runs = {}
        self.cache = {}

    def manifest(self, scene_key):
        return self.asset_json(scene_key,'manifest.json')

    def asset_json(self, scene_key, relpath):
        return json.loads((self.asset_dir/scene_key/relpath).read_text())

    def asset_url(self, scene_key, relpath):
        return f'/api/assets/{scene_key}/{relpath}'

    def list_scenes(self, workspace=None):
        scenes = [json.loads(p.read_text()) for p in self.asset_dir.glob('*/manifest.json')]
        return [s for s in scenes if workspace is None or s['workspace']==workspace]

    def entities(self, scene_key):
        return [Entity.model_validate(e) for e in self.asset_json(scene_key,self.manifest(scene_key)['assets']['entities'])['entities']]

    def search_clips(self, filters, qvec_txt, qvec_vis):
        with duckdb.connect(str(self.index_path),read_only=True) as con:
            cursor=con.execute('SELECT * EXCLUDE (v_txt,v_vis) FROM clips WHERE workspace=? ORDER BY clip_id',[filters.workspace])
            names=[d[0] for d in cursor.description]
            clips=[Clip.model_validate(dict(zip(names,row))) for row in cursor.fetchall()]
        clips=[c for c in clips if (not filters.scene_keys or c.scene_key in filters.scene_keys)
               and (not filters.t_range or c.t0_ms<filters.t_range[1] and c.t1_ms>filters.t_range[0])
               and (not filters.cam_ids or c.cam_id in filters.cam_ids)
               and (not filters.place_id or c.place_id==filters.place_id)]
        for i,c in enumerate(clips,1):
            c.rank_text=i if qvec_txt is not None else None
            c.rank_visual=i if qvec_vis is not None else None
        return clips[:filters.k]

    def events_in(self, scene_key, t0_ms, t1_ms):
        m=self.manifest(scene_key)
        if 'events' not in m['assets']:
            return []
        return [Event.model_validate({**{k:v for k,v in e.items() if k!='id'},'scene_key':scene_key,'event_id':scene_key+':'+e['id']})
                for e in self.asset_json(scene_key,m['assets']['events'])['events'] if e['t0_ms']<t1_ms and e['t1_ms']>t0_ms]

    def moment(self, scene_key, t_ms):
        m=self.manifest(scene_key)
        persons, forklifts=[],[]
        if m['workspace']=='warehouse':
            skeleton=self.asset_json(scene_key,m['assets']['skeleton'])['persons']
            visibility=self.asset_json(scene_key,m['assets']['visibility'])
            for eid,p in skeleton.items():
                if t_ms not in p['t_ms']:
                    continue
                i=p['t_ms'].index(t_ms)
                states=geometry.floor_visibility(visibility,t_ms,p['root_world'][i])
                observed=[]
                for cam in m['cams']:
                    rows=self.asset_json(scene_key,f"{m['assets']['boxes_dir']}/{cam['id']}/{t_ms//10000:05d}.json")['rows']
                    obs=[o for e in self.entities(scene_key) if e.entity_id==eid for o in e.observations if o['cam_id']==cam['id'] and o['t0_ms']<=t_ms<o['t1_ms']]
                    if any(r[0]==t_ms and any(r[1]==o['local_track_id'] for o in obs) for r in rows):
                        observed.append(cam['id'])
                persons.append(PersonAtMoment(entity_id=eid,root_world=p['root_world'][i],root_method=p['root_method'][i],
                                              sigma_m=p['root_sigma_m'][i],xyz17=p['xyz'][i],seen_by=[c for c,s in states.items() if s=='visible'],observed_in=observed))
            for f in self.asset_json(scene_key,m['assets']['forklift'])['forklifts']:
                point=next((p for p in f['track'] if p['t_ms']==t_ms),None)
                if point:
                    forklifts.append(ForkliftAtMoment(entity_id=f['entity_id'],**{k:v for k,v in point.items() if k!='t_ms'}))
        return Moment(t_ms=t_ms,persons=persons,forklift=forklifts,floor_visibility_ref=m['assets'].get('visibility'),
                      cams=[CamFrame(id=c['id'],frame_url=self.asset_url(scene_key,f"{c['thumbs']}/{t_ms//1000:05d}.jpg")) for c in m['cams']])

    def begin_run(self, q, ctx, *, kind='ask', source_run_id=None):
        run_id='r_'+uuid.uuid4().hex
        self.runs[run_id]=AgentRun(run_id=run_id,kind=kind,q=q,context=ctx,status='running',final=None,
                                  created=datetime.now(timezone.utc).isoformat(),elapsed_ms=None,source_run_id=source_run_id,
                                  weave_url=None,model_revs={},steps=[],claims=[],evidence=[],hits=[])
        return run_id

    def append_step(self, run_id, step):
        self.runs[run_id].steps.append(step)

    def update_step(self, run_id, step):
        self.runs[run_id].steps[:]=[step if s.step_id==step.step_id else s for s in self.runs[run_id].steps]

    def add_evidence(self, run_id, evidence):
        existing={e.eid:e for e in self.runs[run_id].evidence}
        existing.update({e.eid:e for e in evidence})
        self.runs[run_id].evidence=list(existing.values())

    def set_hits(self, run_id, hits):
        self.runs[run_id].hits=hits

    def finalize_run(self, run_id, status, claims, *, final, weave_url=None, elapsed_ms=None):
        run=self.runs[run_id]
        run.status,run.claims,run.final,run.weave_url,run.elapsed_ms=status,claims,final,weave_url,elapsed_ms

    def get_run(self, run_id):
        return self.runs.get(run_id)

    def list_runs(self, scene_key=None, limit=20):
        return [r.model_dump(mode='json') for r in reversed(list(self.runs.values())) if scene_key is None or r.context.get('scene_key')==scene_key][:limit]

    def cache_get(self, qhash):
        return self.cache.get(qhash)

    def cache_put(self, qhash, q, context, response, backend, source_run_id):
        self.cache[qhash]={'response':response,'source_run_id':source_run_id}


def fixture_store(tmp_path):
    index=tmp_path/'index.duckdb'
    build_index(index,FIXTURES)
    return FixtureStore(index_path=index,app_path=tmp_path/'app.duckdb',asset_dir=FIXTURES/'sva-out')


def l1_rows(t):
    """dets2d rows at scene time t: one pedestrian throughout, two cars from 5 s."""
    rows=[[t,'person',40+t//50,100,80+t//50,220,0.9]]
    return rows+([[t,'car',300,200,400,260,0.8],[t,'car',420,200,520,260,0.7]] if t>=5000 else [])


def build_l1(root, *, site=False):
    """Minimal stand-in for T1's L1 fixture: 1 cam 640x360, 10 s at 10 fps, one chunk; site=True gives the S3 form
    (s3:// chunk, no video, thumbs or dets files)."""
    import cv2
    import numpy as np
    sdir=root/L1_KEY
    chunk=L1_SITE_CHUNK if site else f'chunks/{L1_CAM}/0000.mp4'
    cam={'kind':'cam2d','id':L1_CAM,'w':640,'h':360,'video':None if site else f'video/{L1_CAM}.mp4',
         'chunks':[{'t0_ms':0,'t1_ms':10000,'uri':chunk}],
         'frame_ref':{'video_w':640,'video_h':360,'thumb_w':320,'thumb_h':180},'offset_ms':0,
         'thumbs':None if site else f'thumbs/{L1_CAM}','sync_method':'single_camera','sync_residual_ms':None}
    m={'schema_version':'1','analysis_rev':'l1-1','producer_commit':'fixture','input_artifact_hashes':{},
       'scene_id':'st/fixture_cam_1/clip01','scene_key':L1_KEY,'workspace':'street','evidence_level':'L1',
       'title':'Street fixture camera (clip01)','t0_ms':0,'t1_ms':10000,'fps':10,'world_frame_id':'image','metric':False,
       'cams':[cam],'assets':{'dets_dir':'dets'},
       'sources':{'vss':{'camera_id':L1_CAM,'location':'fixture','stem':'clip01'}},
       'pipeline':{'models':{'detector':'yolo11_coco (VSS sidecar)','caption':'nvidia/cosmos3-nano-reasoner (VSS)'},'created':'2026-10-09T00:00:00Z'}}
    sdir.mkdir(parents=True)
    (sdir/'manifest.json').write_text(json.dumps(m))
    if site:
        return sdir
    (sdir/'chunks'/L1_CAM).mkdir(parents=True)
    (sdir/'thumbs'/L1_CAM).mkdir(parents=True)
    writer=cv2.VideoWriter(str(sdir/chunk),cv2.VideoWriter_fourcc(*'mp4v'),10,(640,360))
    for i in range(100):
        frame=np.full((360,640,3),40+i,np.uint8)
        for _,_,x1,y1,x2,y2,_ in l1_rows(i*100):
            cv2.rectangle(frame,(x1,y1),(x2,y2),(255,255,255),-1)
        writer.write(frame)
        if i%10==0:
            cv2.imwrite(str(sdir/cam['thumbs']/f'{i//10:05d}.jpg'),cv2.resize(frame,(320,180)))
    writer.release()
    (sdir/'video').mkdir()
    (sdir/cam['video']).write_bytes((sdir/chunk).read_bytes())
    (sdir/'dets'/L1_CAM).mkdir(parents=True)
    (sdir/'dets'/L1_CAM/'0000.json').write_text(json.dumps({'schema_version':'1','analysis_rev':'l1-1','producer_commit':'fixture',
        'input_artifact_hashes':{},'cam_id':L1_CAM,'chunk_index':0,'chunk_t0_ms':0,'source':'yolo11_coco',
        'rows':[r for t in range(0,10000,100) for r in l1_rows(t)]}))
    return sdir


def add_l1_index(index, sdir):
    """Street rows as the index carries them: one clip per 5 s segment; the second segment has no index caption."""
    m=json.loads((sdir/'manifest.json').read_text())
    with duckdb.connect(str(index)) as con:
        con.execute('INSERT INTO scene_manifest VALUES (?, ?, ?, ?, ?, ?, ?)',[m['scene_id'],L1_KEY,'street',m['title'],0,10000,json.dumps(m)])
        for i, (caption, n_person, n_vehicle) in enumerate([('Street fixture camera: a pedestrian walks along the sidewalk',1,0),(None,1,2)]):
            text=caption or f'street segment {i}'
            con.execute('INSERT INTO clips VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                        [f'{L1_KEY}:{L1_CAM}:{i:05d}',m['scene_id'],L1_KEY,'street',L1_CAM,i*5000,i*5000+5000,None,None,None,
                         f'{L1_SITE_CHUNK[:-4].replace("vss-chunks/team-38","vss-chunks-segments/segments")}_segment_{i+1:03d}_of_002.mp4',
                         caption,pseudo_vec('txt:'+text),pseudo_vec('vis:'+text),n_person,n_vehicle,None,None,None,None,None,[],
                         m['cams'][0]['chunks'][0]['uri']])


def l1_assets(tmp_path, *, site=False):
    """(index, asset_dir): the committed fixture scenes plus the L1 scene, for a real Store or the app."""
    import shutil
    # a copy, not symlinks: Store.local_asset refuses files that resolve outside ASSET_DIR
    assets=shutil.copytree(FIXTURES/'sva-out',tmp_path/'sva-out')
    sdir=build_l1(assets,site=site)
    index=tmp_path/'index.duckdb'
    build_index(index,FIXTURES)
    add_l1_index(index,sdir)
    return index, assets


def fixture_l1_store(tmp_path, *, site=False):
    index, assets=l1_assets(tmp_path,site=site)
    return FixtureStore(index_path=index,app_path=tmp_path/'app.duckdb',asset_dir=assets)


class MemoryStep:
    def __init__(self, run, name, inputs, state, parent):
        self.run=run
        self.i=run.next_i
        run.next_i+=1
        self.step_id=f'{run.run_id}.s{self.i}'
        self.step=RunStep(step_id=self.step_id,parent_step_id=parent,i=self.i,state=state,name=name,
                          started_ms=round(time.monotonic()*1000-run.t0_monotonic_ms),elapsed_ms=0,input=inputs,output=None,
                          evidence_ids=[],exec_state='live',trace_delivery_state='local')
        run.store.append_step(run.run_id,self.step)

    def output(self,value): self.step.output=value
    def evidence(self,eids):
        self.step.evidence_ids=eids
        self.run.read_eids.update(eids)
    def note(self,text): self.step.note=text
    def exec_state(self,state): self.step.exec_state=state


def install_trace(monkeypatch):
    def start_run(store,q,context,*,kind='ask',source_run_id=None):
        return trace.RunHandle(store.begin_run(q,context,kind=kind,source_run_id=source_run_id),store,q,context,
                               kind=kind,t0_monotonic_ms=time.monotonic()*1000)
    @contextmanager
    def traced(run,name,inputs,*,state,parent_step_id=None):
        step=MemoryStep(run,name,inputs,state,parent_step_id)
        try:
            yield step
        except BaseException:
            step.exec_state('failed')
            raise
        finally:
            step.step.elapsed_ms=round(time.monotonic()*1000-run.t0_monotonic_ms-step.step.started_ms)
            run.store.update_step(run.run_id,step.step)
    def finish_run(run,status,*,final,claims):
        run.store.finalize_run(run.run_id,status,claims,final=final,elapsed_ms=round(time.monotonic()*1000-run.t0_monotonic_ms))
        return None
    monkeypatch.setattr(trace,'start_run',start_run)
    monkeypatch.setattr(trace,'traced',traced)
    monkeypatch.setattr(trace,'finish_run',finish_run)
    from app.api import actions
    monkeypatch.setattr(actions,'trigger',lambda store,req:TriggerResult(action_id='a_fixture',delivered=True,
                                                                       review_task_id='review_fixture',zone_id='z_fixture',existing=False))


def install_vlm(monkeypatch):
    tasks={'situation':'C1','predict_next':'C2','ground_objects':'C3','localize':'C4','describe_region':'C5'}
    def recorded(task):
        def call(scene_key,*args,run=None,**kwargs):
            records=json.loads((FIXTURES/'sva-out'/scene_key/'cosmos'/f'{scene_key}.json').read_text())['records']
            record=next((r for r in records if r['task']==task),None)
            if record is None:
                raise vlm.VLMUnavailable('no recorded fixture')
            return CosmosRecord.model_validate(record)
        return call
    for name,task in tasks.items():
        monkeypatch.setattr(vlm,name,recorded(task))
