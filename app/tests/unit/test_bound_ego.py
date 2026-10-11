import math
import pytest
from app.api import agent
from app.core import tools,live_measure
from app.core.models import EntityRef
from app.tests.unit.test_semantic_repairs import store,ask,final
from app.tests.unit.test_live_measure import store_with,SIDECAR,ROW

KEY='fl-pie-fixture-01'

def test_bound_pedestrian_ego_distance_uses_named_actor_and_actual_sample(store,monkeypatch):
    monkeypatch.setattr(live_measure,'measure',lambda *a,**k:(_ for _ in ()).throw(AssertionError('Anonymous live proxy must not be used')))
    frame=next(f for f in store.asset_json(KEY,'ego/00000.json')['frames'] if any(p['entity_id']=='P1' for p in f['peds']))
    tm=frame['t_ms']
    p=next(p for p in frame['peds'] if p['entity_id']=='P1')
    target=EntityRef(scene_key=KEY,entity_id='P1',kind='person')
    result=tools.bound_ego_distance(store,KEY,(tm,tm+1),target)
    assert result.evidence[0].value['distance_m']==pytest.approx(math.hypot(p['x'],p['y']))
    assert result.evidence[0].value['target_entity_id']=='P1' and result.evidence[0].value['reference_entity']=='ego'
    frames=ask(store,f'How far is P1 from the recording car at {tm/1000} seconds?','fleet',scene_key=KEY)
    assert final(frames)=='SUPPORTED'
    assert 'P1' in next(f['data']['text'] for f in frames if f['event']=='answer')

@pytest.mark.parametrize('target,zone',[('P999',None),('P1',[(1000,1000),(1001,1000),(1001,1001)])])
def test_bound_ego_distance_never_substitutes_another_pedestrian(store,target,zone):
    result=tools.bound_ego_distance(store,KEY,(0,10000),EntityRef(scene_key=KEY,entity_id=target,kind='person'),zone=zone)
    assert not result.evidence and result.envelope.output['status']=='UNRESOLVED'


def test_scalar_recording_vehicle_speed_is_not_a_speeding_claim(store):
    frames=ask(store,'How fast is the recording car at 1 seconds?','fleet',scene_key=KEY)
    assert final(frames)=='SUPPORTED'
    answer=next(f['data'] for f in frames if f['event']=='answer')
    assert 'OBD speed' in answer['text'] and 'uncertainty is unavailable' in answer['text']
    assert all(r['field'] in {'value','t0_ms'} for r in answer['measure_refs'])


def test_scalar_ego_speed_cannot_answer_selected_person(store):
    frames=ask(store,'How fast is the recording car at 1 seconds?','fleet',scene_key=KEY,selection=['P1'])
    assert final(frames)=='UNRESOLVED'
    assert not any(f['event']=='tool_call' and f['data']['name']=='ego_speed' for f in frames)


def test_live_warehouse_proxy_cannot_claim_a_named_forklift():
    s=store_with(SIDECAR)
    result=live_measure.measure(s,'wh-test','cam_a',20000,20200,rows=[ROW],target_ids=['P1','F1'])
    assert result['value_m'] is None and result['reason']=='live_target_binding_unverified'
    generic=live_measure.measure(s,'wh-test','cam_a',20000,20200,rows=[ROW])
    assert generic['value_m'] is not None and generic['target_class']=='vehicle_proxy' and not generic['binding_verified']


def test_scalar_ego_speed_cannot_assert_a_threshold_without_comparison(store):
    frames=ask(store,'How fast is the recording car above 20 km/h at 1 seconds?','fleet',scene_key=KEY)
    assert final(frames)=='UNRESOLVED'
    assert not any(f['event']=='tool_call' and f['data']['name']=='ego_speed' for f in frames)
