"""Regressions for conditions previously discarded while returning SUPPORTED."""
import pytest

from app.api import agent
from app.core import embed, llm, street, vlm
from app.tests.unit.a5_fakes import L1_KEY, L1_CAM, fixture_l1_store, install_trace, install_vlm
from pipeline.common.schema_check import check_sse_sequence


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = fixture_l1_store(tmp_path)
    install_trace(monkeypatch)
    install_vlm(monkeypatch)
    monkeypatch.setattr(embed,'embed_text',lambda q:[0.0]*256)
    monkeypatch.delenv('LLM_URL',raising=False)
    monkeypatch.setattr(vlm,'localize',lambda *a,**k: (_ for _ in ()).throw(vlm.VLMUnavailable('offline')))
    return s


def ask(store,q,workspace='warehouse',**context):
    context = {'workspace':workspace,'scene_key':L1_KEY if workspace=='street' else 'wh-sdg-fixture-01',**context}
    frames = list(agent.ask_frames(store,agent.AskRequest(q=q,context=context,budget={'tools':16,'seconds':45})))
    assert check_sse_sequence(frames)==[]
    return frames


def final(frames):
    return next(f['data']['final'] for f in frames if f['event']=='status')


@pytest.mark.parametrize('q,condition',[
    ('Did the worker not dodge the forklift?','negation'),
    ('Did every worker dodge the forklift?','exhaustive_coverage'),
    ('Did no worker dodge the forklift?','exhaustive_coverage'),
    ('Did the worker with a red hardhat dodge the forklift?','attribute:red'),
])
def test_unsupported_conditions_do_not_become_supported(store,q,condition):
    frames = ask(store,q)
    assert final(frames)=='UNRESOLVED'
    claims = [f['data'] for f in frames if f['event']=='claim']
    assert any(p['operator']==condition and p['status']=='unknown' for c in claims for p in c['predicates'])


@pytest.mark.parametrize('q',['When does a pedestrian cross?','Did a person wearing a red hardhat cross the street?'])
def test_l1_presence_does_not_prove_crossing(store,q):
    frames = ask(store,q,'street')
    assert final(frames)=='UNRESOLVED'
    claims = [f['data'] for f in frames if f['event']=='claim']
    assert any((c.get('explain') or {}).get('abstain',{}).get('reason')=='no_geometry' for c in claims)
    assert all(not f['data']['measure_refs'] for f in frames if f['event']=='answer')


@pytest.mark.parametrize('q,expected',[
    ('When did the worker dodge after 7 seconds?',(7001,2**53-1)),
    ('When did the worker dodge before 4 seconds?',(0,4000)),
])
def test_one_sided_time_is_a_constraint(q,expected):
    assert llm.rule_parse(q,{'workspace':'warehouse'}).t_range==expected


def test_model_event_is_not_independent_proof(store,monkeypatch):
    original = store.events_in
    monkeypatch.setattr(store,'events_in',lambda *args:[e.model_copy(update={'source':'model'}) for e in original(*args)])
    frames = ask(store,'When did the worker dodge the forklift?')
    assert final(frames)=='UNRESOLVED'
    assert all(not f['data']['measure_refs'] for f in frames if f['event']=='answer')


def test_location_does_not_claim_visibility_is_position(store):
    frames = ask(store,'Where is P2?',t_ms=2100)
    assert final(frames)=='SUPPORTED'
    assert any('scene world ground coordinates' in f['data']['text'] for f in frames if f['event']=='answer')
    assert any(e['method']=='ground_position' for f in frames if f['event']=='tool_result' for e in f['data'].get('evidence',[]))
    assert not any(f['event']=='tool_call' and f['data']['name']=='visibility' for f in frames)


@pytest.mark.parametrize('q,method,words',[
    ('How many people walk past?','bytetrack_people_count',['person tracks','0–10 s']),
    ('When is the street busiest?','yolo_people_peak',['peak','person detections','0–10 s']),
])
def test_street_statistics_measure_whole_window_and_cite_numbers(store,q,method,words):
    frames = ask(store,q,'street')
    assert final(frames)=='SUPPORTED'
    results = [f['data'] for f in frames if f['event']=='tool_result']
    assert len(results)==1 and results[0]['envelope']['input_range']['t1_ms']==10000
    evidence = {e['eid']:e for r in results for e in r['evidence']}
    assert {e['method'] for e in evidence.values()}=={method}
    answer = next(f['data'] for f in frames if f['event']=='answer')
    assert all(w in answer['text'] for w in words)
    assert answer['measure_refs'] and all(evidence[r['eid']]['kind']=='measure' for r in answer['measure_refs'])
    assert not any(f['event']=='tool_call' and f['data']['name']=='search' for f in frames)


def test_byte_tracking_counts_continuous_person_once_and_new_person_twice(store,monkeypatch):
    rows = [[t,'person',10,20,40,80,.95] for t in range(0,1000,100)]
    rows += [[t,'person',200,20,230,80,.95] for t in range(400,1000,100)]
    monkeypatch.setattr(street,'detections',lambda *a:{'rows':rows,'frame_times':list(range(0,1000,100)),
                                                     'source':'test','gaps':[],'frames_read':10,'exec_state':'precomputed'})
    stats = street.people_statistics(store,L1_KEY,L1_CAM,0,1000,'tracked_count')
    assert stats.envelope.output['count']==2
    peak = street.people_statistics(store,L1_KEY,L1_CAM,0,1000,'peak_count')
    assert peak.envelope.output['count']==2 and peak.envelope.output['t_s']==.4


def test_missing_frames_prevent_full_window_count_proof(store,monkeypatch):
    original = street.detections
    def incomplete(*a):
        d = original(*a)
        return {**d,'gaps':[[2000,3000,'no_frames']]}
    monkeypatch.setattr(street,'detections',incomplete)
    frames = ask(store,'How many people walk past?','street')
    assert final(frames)=='UNRESOLVED'
    assert 'Missing frames' in next(f['data']['text'] for f in frames if f['event']=='answer')


def test_selected_entity_and_time_cannot_answer_another_worker(store):
    frames = ask(store,'When did the worker dodge the forklift?',t_ms=7000,selection=['P2'])
    assert final(frames)!='SUPPORTED'
    assert all('4.6 s' not in f['data']['text'] for f in frames if f['event']=='answer')


def test_after_bound_cannot_return_earlier_event_start(store):
    frames = ask(store,'When did the worker dodge the forklift after 7 seconds?')
    assert final(frames)!='SUPPORTED'
    assert all('4.6 s' not in f['data']['text'] for f in frames if f['event']=='answer')


def test_explicit_ids_use_full_interval_closest_path(store):
    frames = ask(store,'How close did P2 come to F1?')
    assert final(frames)=='SUPPORTED'
    calls = [f['data'] for f in frames if f['event']=='tool_call']
    assert calls[0]['name']=='closest_approach'
    assert next(c['args']['a']['entity_id'] for c in calls if c['name']=='measure')=='P2'


def test_tracker_uses_detection_sampling_rate_not_video_fps(store,monkeypatch):
    rows = [[t,'person',10,20,40,80,.95] for t in range(0,4000,1000)]
    monkeypatch.setattr(street,'detections',lambda *a:{'rows':rows,'frame_times':list(range(0,4000,1000)),
                                                     'source':'test','gaps':[],'frames_read':4,'exec_state':'precomputed'})
    rates = []
    original = street.live_measure.track
    def track(frames,fps,labels):
        rates.append(fps)
        return original(frames,fps,labels)
    monkeypatch.setattr(street.live_measure,'track',track)
    stats = street.people_statistics(store,L1_KEY,L1_CAM,0,4000,'tracked_count')
    assert rates==[1.] and stats.envelope.output['count']==1


def test_moving_measurement_does_not_satisfy_a_running_person_condition(store):
    frames = ask(store,'Did the running worker dodge the moving forklift?')
    assert final(frames)=='UNRESOLVED'
    assert any(p['operator']=='action_modifier:running' and p['status']=='unknown'
               for f in frames if f['event']=='claim' for p in f['data']['predicates'])


def test_person_statistics_do_not_answer_a_car_count(store):
    frames = ask(store,'How many cars are there?','street')
    assert final(frames)=='UNRESOLVED'


def test_count_does_not_ignore_a_requested_zone(store):
    frames = ask(store,'How many people walk past?','street',zone=[[-1.,-1.],[1.,-1.],[1.,1.]])
    assert final(frames)=='UNRESOLVED'


@pytest.mark.parametrize('q',['How many people crossed the street?','How many people were within 2 meters of the car?'])
def test_count_does_not_drop_action_or_distance_conditions(store,q):
    frames = ask(store,q,'street')
    assert final(frames)=='UNRESOLVED'


@pytest.mark.parametrize('phrase,quantifier',[
    ('all the workers','all'),('all of the workers','all'),('all of workers','all'),
    ('each of the workers','all'),('each one of the workers','all'),
    ('every one of the workers','all'),('every worker','all'),
    ('none of the workers','none'),('none of these workers','none'),
    ('every video','all'),('every recording','all'),('all the recordings','all'),
    ('both workers','all'),('all these workers','all'),('every single worker','all'),
])
def test_common_quantifier_phrases_are_retained(phrase,quantifier):
    parsed = llm.rule_parse('Show '+phrase+' where a worker dodges the forklift',{'workspace':'warehouse'})
    assert parsed.quantifier==quantifier and 'exhaustive_coverage' in parsed.constraints


@pytest.mark.parametrize('q',[
    'Did all the workers dodge the forklift?',
    'Did all of the workers dodge the forklift?',
    'Did each of the workers dodge the forklift?',
    'Show every video where a worker dodges the forklift',
    'Show every recording where a worker dodges the forklift',
])
def test_universal_variants_cannot_be_proved_by_one_retrieved_worker(store,q):
    frames = ask(store,q)
    assert final(frames)=='UNRESOLVED'


@pytest.mark.parametrize('q,condition',[
    ('Did two workers dodge the forklift?','entity_count'),
    ('Did 2 workers dodge the forklift?','entity_count'),
    ('Did a worker dodge the forklift twice?','occurrence_count'),
    ('Did a worker dodge the forklift for 5 seconds?','duration'),
    ('When did a worker dodge the forklift after 00:07?','temporal_relation'),
])
def test_count_duration_and_unparsed_time_conditions_cannot_disappear(store,q,condition):
    assert condition in llm.rule_parse(q,{'workspace':'warehouse'}).constraints
    assert final(ask(store,q))=='UNRESOLVED'


@pytest.mark.parametrize('q,expected',[
    ('When did the moving worker dodge the forklift, and how far was it?','UNRESOLVED'),
    ('When did the moving worker dodge the moving forklift, and how far was it?','UNRESOLVED'),
    ('When did the worker dodge the moving forklift, and how far was it?','SUPPORTED'),
])
def test_moving_modifier_uses_the_described_actor(store,monkeypatch,q,expected):
    from app.core import tools
    original_moment,original_measure = store.moment,tools.measure
    def stationary_person(key,t):
        moment = original_moment(key,t)
        return moment.model_copy(update={'persons':[p.model_copy(update={'root_world':(0.,0.)}) if p.entity_id=='P1' else p for p in moment.persons]})
    monkeypatch.setattr(store,'moment',stationary_person)
    speed_reads = []
    def certain_vehicle_speed(s,key,t,a,b,kind,**kw):
        result = original_measure(s,key,t,a,b,kind,**kw)
        if kind=='speed':
            speed_reads.append(a.entity_id)
            result.evidence = [e.model_copy(update={'value':10.,'sigma':.1}) for e in result.evidence]
        return result
    monkeypatch.setattr(tools,'measure',certain_vehicle_speed)
    frames = ask(store,q)
    assert final(frames)==expected
    assert speed_reads==(['F1'] if expected=='SUPPORTED' else [])


@pytest.mark.parametrize('q',[
    'When did the worker dodge the forklift between 7 and 9 seconds?',
    'When did the worker dodge the forklift during the first 3 seconds?',
    'When did the worker dodge the forklift in the last 2 seconds?',
    'When did the worker dodge the forklift at 00:07?',
    'When did the worker dodge the forklift from 00:07 to 00:09?',
])
def test_common_time_scopes_do_not_support_an_earlier_event(store,q):
    frames = ask(store,q)
    assert final(frames)!='SUPPORTED'
    assert all('4.6 s' not in f['data']['text'] for f in frames if f['event']=='answer')


def test_between_time_has_actual_search_bounds():
    parsed = llm.rule_parse('When did the worker dodge between 7 and 9 seconds?',{'workspace':'warehouse'})
    assert parsed.t_range==(7000,9000)
    assert 'temporal_relation' not in parsed.constraints


def test_default_blind_spot_during_window_remains_explicit():
    parsed = llm.rule_parse('Is there anyone in the ceiling_00 blind spot during these 10 seconds?',{'workspace':'warehouse'})
    assert parsed.t_range==(0,10000) and 'temporal_relation' not in parsed.constraints


@pytest.mark.parametrize('q,context,expected',[
    ('Where is P2?',{'selection':['P2']},'UNRESOLVED'),
    ('Where is P2?',{},'UNRESOLVED'),
    ('Where is the worker?',{},'UNRESOLVED'),
    ('Where is this car?',{'zone':[[0.,0.],[1.,0.],[1.,1.]]},'UNRESOLVED'),
    ('Where is the building?',{},'UNRESOLVED'),
    ('Where is this car?',{},'SUPPORTED'),
])
def test_camera_free_gps_only_locates_the_recording_vehicle(store,monkeypatch,q,context,expected):
    original = store.manifest
    monkeypatch.setattr(store,'manifest',lambda key:{**original(key),'cams':[]} if key=='fl-pie-fixture-01' else original(key))
    frames = ask(store,q,'fleet',scene_key='fl-pie-fixture-01',t_ms=1000,**context)
    assert final(frames)==expected
    calls = [f['data']['name'] for f in frames if f['event']=='tool_call']
    assert calls==(['gps_position'] if expected=='SUPPORTED' else [])
