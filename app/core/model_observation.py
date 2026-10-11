"""Visual assertions with actual frame provenance, separate from measurements."""
import hashlib
import json
import re
import time

from app.core import vlm
from app.core.models import EntityRef, Evidence, ModelObservation
from app.core.tools import result


def observe(store, key, cam, t_range, actor_refs, *, run=None):
    try:
        return _observe(store, key, cam, t_range, actor_refs, run=run)
    except (vlm.VLMUnavailable, ValueError) as exc:
        lo, hi = t_range
        return result(key, lo, hi, {'observation': None, 'status': 'UNRESOLVED'}, cam=cam,
                      exec_state='failed', quality='unavailable', error=str(exc),
                      gaps=[(lo, hi, 'model_observation_unavailable')])


def _observe(store, key, cam, t_range, actor_refs, *, run=None):
    started = time.monotonic()
    actors = [a if isinstance(a, EntityRef) else EntityRef.model_validate(a) for a in actor_refs]
    if any(a.scene_key != key for a in actors):
        raise ValueError('Observation subjects must belong to the input scene')
    m = store.manifest(key)
    lo, hi = t_range
    if not (m['t0_ms'] <= lo < hi <= m['t1_ms']) or cam not in {c['id'] for c in m['cams']}:
        raise ValueError('Observation scope is outside the source')
    raw = vlm.observe_window(key, cam, [lo, hi], actors, run=run)
    if (raw['cam_id'], raw['t0_ms'], raw['t1_ms']) != (cam, lo, hi):
        raise ValueError('Observation receipt differs from requested scope')
    frames = raw['input_frame_refs']
    if not frames or any(f['cam_id'] != cam or not lo <= f['t_ms'] < hi for f in frames):
        raise ValueError('Observation contains an out-of-scope frame')
    facts = []
    for fact in raw['facts']:
        text = fact['text']
        # Entity identifiers are references. Every other numeric token belongs
        # to the measurement channel, which has its own verifier.
        stripped = re.sub(r'\b(?:' + '|'.join(re.escape(a.entity_id) for a in actors) + r')\b', '', text) if actors else text
        if re.search(r'\d', stripped) or re.search(r'\b(?:one|two|three|four|five|six|seven|eight|nine|ten|several|many)\s+(?:people|persons?|workers?|pedestrians?|vehicles?|cars?|forklifts?|met(?:er|re)s?|seconds?|times)\b', stripped, re.I):
            raise ValueError('Numeric visual claims require a measurement')
        if not fact['frame_refs'] or any(f not in frames for f in fact['frame_refs']):
            raise ValueError('Observation fact cites an unread frame')
        if not set(fact['subject_ids']) <= {a.entity_id for a in actors}:
            raise ValueError('Observation fact cites an unknown subject')
        facts.append({k: fact[k] for k in ('subject_ids', 'text', 'frame_refs', 'uncertainty')})
    record = dict(scene_key=key, cam_id=cam, t0_ms=lo, t1_ms=hi, frame_refs=frames,
                  subject_refs=[a.model_dump(mode='json') for a in actors], facts=facts,
                  model_rev=raw['model_rev'], prompt_rev=raw['prompt_rev'],
                  verification='unverified', input_digest=raw['input_digest'])
    digest = hashlib.sha256(json.dumps(record, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    record['observation_id'] = 'o_' + digest[:24]
    ev = Evidence.create(scene_key=key, cam_id=cam, t0_ms=lo, t1_ms=hi, observation_ids=[],
                         analysis_rev=m['analysis_rev'], kind='model', method='visual_observation',
                         params={'content_digest': digest}, value=record)
    record['eid'] = ev.eid
    record = ModelObservation.model_validate(record).model_dump(mode='json')
    ev = ev.model_copy(update={'value': dict(record)})
    return result(key, lo, hi, {'observation': record}, cam=cam, evidence=[ev],
                  pts_read=len(frames), model_rev=raw['model_rev'], quality='unverified',
                  exec_state=raw['exec_state'], started=started)
