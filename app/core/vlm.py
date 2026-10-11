"""Cosmos3 Reasoner client (C1-C5). Signatures are the W0 contract; P-X implements, A5 tools call.

Shared input/output rules (§2.5): 4 fps frames with `t=12.50s` burned at the bottom; tracked ids (P3, F1) drawn on frames;
C1/C3/C4/C5 use a +-3 s window; C2 sends only frames in [t_ms - 2000, t_ms) and records max_frame_ms <= t_ms.
Every call returns a CosmosRecord validated against schemas/cosmos.json#/$defs/record, is cached by record_id and,
when `run` is given, is traced as vlm.<task>. Failures raise VLMUnavailable (the caller marks the tool failed).

Frames come from the scene's published videos: dev reads ASSET_DIR/<scene_key>/, site reads presigned S3 URLs of
sva/<scene_key>/ in ASSET_BUCKET. Endpoint: VLM_URL (OpenAI compatible, .../v1), VLM_MODEL, GPU_BEARER_TOKEN if set.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import math
import os
import re
import time
import tempfile
from contextlib import nullcontext
from functools import lru_cache, wraps
from pathlib import Path
from typing import Any

import cv2
import httpx
from jsonschema import Draft202012Validator
from PIL import Image, ImageDraw, ImageFont

from app.core import trace
from app.core.deadline import DeadlineExceeded, remaining, scope
from app.core.models import ENTITY_ID_RE, CosmosRecord, EntityRef, make_record_id
from app.core.street import _digest as street_digest
from app.settings import Settings

log = logging.getLogger(__name__)
# prompt revision per task, part of every record_id: bump only the prompt that changed so other records stay found
PROMPT_REV = {"C1": "px-2", "C1-fleet": "px-1", "C2": "px-1", "C3": "px-1", "C4": "px-1", "C5": "px-1",
              "C3-street": "st-1", "C4-street": "st-1"}
STEP_MS = 250  # 4 fps, the rate Cosmos temporal prompts expect
WINDOW_MS = 3000
PREFIX_MS = 2000
MAX_FRAMES = 24  # longer windows are sampled sparser, never sent as more frames
FRAME_W = 640  # about 225 prompt tokens per frame; a 10 s window stays well inside the 32k context
TIMEOUT_S = 120
BOX_MATCH_MS = 100  # a track row counts for a frame when it is this close in time
NEAR_MS = 250  # half the 500 ms minimum spacing of cosmos_batch moments, so at most one pre-run record is this close
WH_LABELS = ["yielding", "interaction", "passing", "unrelated"]  # the C1 prompt asks for the first that fits, in this order
FL_LABELS = ["crossing", "waiting", "walking_along", "unrelated"]
LABEL_DEF = {
    "yielding": "at some point the person makes way for the forklift: steps aside, backs away, stops or waits while it "
                "goes by; this holds even when they are close to each other; being hit or pushed by the forklift is "
                "not yielding",
    "interaction": "the forklift is moving and comes within about 2 m (less than a forklift length) of the person, and "
                   "the person does not make way",
    "passing": "the person and the forklift stay more than about 2 m apart",
    "unrelated": "none of the above, e.g. the forklift stands still next to the person or one of them is not visible",
    "crossing": "the pedestrian crosses the road in front of the camera car",
    "waiting": "the pedestrian stands at the curb or crossing without crossing",
    "walking_along": "the pedestrian walks along the road without crossing",
}
FL_LABEL_DEF = {**LABEL_DEF, "unrelated": "no relation between them in this window"}  # fleet C1 text of PROMPT_REV["C1-fleet"]
ACTIONS = ["stop", "slow", "turn", "continue"]
_SCENE_FIELDS = {  # the fixed observation JSON of §2.5, asked for in C1 and C2 and kept in record.raw
    # maxItems: without a cap the model can repeat list items until max_tokens cuts the JSON
    "subjects": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
    "action": {"type": "string"},
    "state_before": {"type": "string"},
    "state_after": {"type": "string"},
    "visible_interactions": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
    "source_intervals": {"type": "array", "items": {"type": "array", "items": {"type": "number"}, "minItems": 2,
                                                    "maxItems": 2}, "maxItems": 4},
}
_fails: dict[Any, int] = {}
_opened: dict[Any, float] = {}
CIRCUIT_COOLDOWN_S = 30.0  # consecutive endpoint failures per run handle (these dicts: calls without a run)


class VLMUnavailable(RuntimeError):
    """Endpoint down, circuit open (2 consecutive 502/timeouts), or no cached record in cache-only mode."""


def _budgeted(function):
    @wraps(function)
    def call(*args,**kwargs):
        try:
            with scope(getattr(kwargs.get('run'),'deadline',None)):
                return function(*args,**kwargs)
        except DeadlineExceeded as exc:
            raise VLMUnavailable(str(exc)) from exc
        except (httpx.HTTPError, OSError, json.JSONDecodeError, UnicodeDecodeError, cv2.error) as exc:
            raise VLMUnavailable(f"model input unavailable: {type(exc).__name__} {exc}") from exc
    return call


@_budgeted
def situation(scene_key: str, t_ms: int, actor_refs: list[EntityRef], *, cam: str | None = None, t_range: tuple[int, int] | list[int] | None = None,
              run: Any = None) -> CosmosRecord:
    """C1: label in situation_map labels + one-sentence reason + uncertainty."""
    labels = FL_LABELS if _fleet(scene_key) else WH_LABELS
    ids = [a.entity_id for a in actor_refs]
    defs = "; ".join(f"{k}: {(FL_LABEL_DEF if _fleet(scene_key) else LABEL_DEF)[k]}" for k in labels)
    order = "" if _fleet(scene_key) else "Check the labels in the order given and take the first that fits. "
    prompt = (f"Classify the physical situation between {', '.join(ids)} around t={t_ms / 1000:.2f}s. Labels: {defs}. "
              f"{order}Base the label on visible motion only; reason is one sentence citing what is visible; "
              "uncertainty is 0 (sure) to 1 (guess).")
    schema = _obj({**_SCENE_FIELDS, "label": {"enum": labels}, "reason": {"type": "string"},
                   "uncertainty": {"type": "number", "minimum": 0, "maximum": 1}})
    window = (t_ms - WINDOW_MS, t_ms + WINDOW_MS)
    if t_range is not None:
        hard = tuple(t_range)
        if len(hard) != 2 or hard[0] < 0 or hard[1] <= hard[0]:
            raise VLMUnavailable("invalid_model_scope")
        window = (max(window[0], hard[0]), min(window[1], hard[1]))
    if window[1] <= window[0]:
        raise VLMUnavailable("invalid_model_scope")
    extra = [f"scope:{window[0]}:{window[1]}"] if t_range is not None else []
    return _call("C1", scene_key, cam, t_ms, actor_refs, window, extra, prompt, schema,
                 lambda o, f: {"label": o["label"], "reason": o["reason"], "uncertainty": o["uncertainty"]}, run)


@_budgeted
def predict_next(scene_key: str, t_ms: int, actor_ref: EntityRef, horizon_s: float = 2, *, run: Any = None) -> CosmosRecord:
    """C2: prefix-only; payload.actions subset of stop|slow|turn|continue, payload.max_frame_ms <= t_ms."""
    who = (f"the camera car (ego vehicle), with {actor_ref.kind} {actor_ref.entity_id} in view" if _fleet(scene_key)
           else f"{actor_ref.kind} {actor_ref.entity_id}")
    prompt = (f"The frames end before t={t_ms / 1000:.2f}s; nothing after that is shown. Predict what {who} does in the "
              f"next {horizon_s:g} s: one or more of stop (comes to rest), slow (clearly decelerates), turn (changes "
              "heading by 15 degrees or more), continue (none of these). reason is one sentence citing visible cues.")
    # vLLM guided decoding rejects uniqueItems, so duplicates are dropped here
    schema = _obj({**_SCENE_FIELDS, "actions": {"type": "array", "items": {"enum": ACTIONS}, "minItems": 1},
                   "reason": {"type": "string"}})
    payload = lambda o, frames: {"actions": list(dict.fromkeys(o["actions"])), "reason": o["reason"], "horizon_s": horizon_s,  # noqa: E731
                                 "max_frame_ms": max(f[0] for f in frames), "actual": None, "ghost": None}
    return _call("C2", scene_key, None, t_ms, [actor_ref], (t_ms - PREFIX_MS, t_ms), [f"h{horizon_s:g}"], prompt, schema,
                 payload, run)


@_budgeted
def ground_objects(scene_key: str, cam: str, t_ms: int, vocab: list[str], *, run: Any = None) -> CosmosRecord:
    """C3: 0-1000 boxes for open-vocabulary occluders; ground_poly and depth check filled by geometry, not the model.
    A street camera has no geometry, so its prompt asks for the objects alone."""
    what = "" if _street(scene_key) else " that could hide a person from this camera"
    prompt = (f"Locate every {', '.join(vocab)} in the frame stamped t={t_ms / 1000:.2f}s{what}. "
              "box_2d is [x1, y1, x2, y2] normalised to 0-1000 of that frame; label is one of the listed words. "
              "Return an empty list when there is none.")
    schema = _obj({"objects": {"type": "array", "items": _obj({"label": {"enum": vocab}, "box_2d": {
        "type": "array", "items": {"type": "number", "minimum": 0, "maximum": 1000}, "minItems": 4, "maxItems": 4}})}})
    payload = lambda o, f: {"objects": [{"label": b["label"], "box_0_1000": b["box_2d"], "ground_poly": [],  # noqa: E731
                                         "depth_residual_m": None, "geometric_support": False} for b in o["objects"]]}
    return _call("C3", scene_key, cam, t_ms, [], (t_ms - WINDOW_MS, t_ms + WINDOW_MS), sorted(vocab), prompt, schema,
                 payload, run)


@_budgeted
def localize(scene_key: str, cam: str, t0_ms: int, t1_ms: int, query: str, *, run: Any = None) -> CosmosRecord:
    """C4: segments [{start_s, end_s, text}] in scene seconds."""
    if type(t0_ms) is not int or type(t1_ms) is not int or t0_ms < 0 or t1_ms <= t0_ms:
        raise VLMUnavailable("invalid_model_scope")
    prompt = (f"Question: {query}? Give each time segment where it happens as start_s and end_s, read from the timestamps "
              "printed on the frames, with a few words of text. Return an empty list if it does not happen in these frames.")
    schema = _obj({"segments": {"type": "array", "items": _obj(
        {"start_s": {"type": "number"}, "end_s": {"type": "number"}, "text": {"type": "string"}})}})
    return _call("C4", scene_key, cam, (t0_ms + t1_ms) // 2, [], (t0_ms, t1_ms), [query, f"{t0_ms}-{t1_ms}"], prompt,
                 schema, lambda o, f: {"query": query, "segments": o["segments"]}, run)


@_budgeted
def describe_region(scene_key: str, cam: str, t_ms: int, region_ref: str, *, run: Any = None) -> CosmosRecord:
    """C5: description of one tracked region (SoM id) only; verification stays unverified until labels exist."""
    prompt = (f"Describe only the region labelled {region_ref} in the frames: what it is, its appearance and what it does. "
              "Ignore everything else. sign_text is any readable sign or lettering inside that region, else null.")
    schema = _obj({"text": {"type": "string"}, "sign_text": {"type": ["string", "null"]}})
    return _call("C5", scene_key, cam, t_ms, [], (t_ms - WINDOW_MS, t_ms + WINDOW_MS), [region_ref], prompt, schema,
                 lambda o, f: {"region_ref": region_ref, "text": o["text"], "sign_text": o["sign_text"]}, run,
                 region=region_ref)


# ---------------------------------------------------------------------------------------------------------------------

def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _fleet(scene_key: str) -> bool:
    return scene_key.startswith("fl-")


def _street(scene_key: str) -> bool:
    return _manifest(scene_key)["workspace"] == "street"


def _rev(task: str, scene_key: str) -> str:
    if task in ("C3", "C4") and _street(scene_key):
        return PROMPT_REV[f"{task}-street"]
    return PROMPT_REV["C1-fleet" if task == "C1" and _fleet(scene_key) else task]


def _call(*args, **kwargs):
    run = kwargs.get('run',args[10] if len(args)>10 else None)
    try:
        with scope(getattr(run,'deadline',None)):
            return _call_impl(*args,**kwargs)
    except DeadlineExceeded as exc:
        raise VLMUnavailable(str(exc)) from exc


def _call_impl(task, scene_key, cam, t_ms, actor_refs, window, key_extra, prompt, schema, make_payload, run, region=None):
    ids = [a.entity_id for a in actor_refs] + ([region] if region and ENTITY_ID_RE.match(region) else [])
    t_ms = int(t_ms)
    def id_at(t):  # key_extra: query/vocab/region/horizon
        return make_record_id(scene_key, task, t, cam, ids + key_extra, _rev(task, scene_key))
    rid = id_at(t_ms)
    identity = _cache_identity(scene_key)
    manifest = _manifest(scene_key)
    bounds = (max(window[0], manifest["t0_ms"]), min(window[1], manifest["t1_ms"]))
    if bounds[1] <= bounds[0] or not manifest['cams'] or cam is not None and not any(c['id']==cam for c in manifest['cams']):
        raise VLMUnavailable('invalid_model_scope: empty window or unknown camera')
    expected_cam = cam or _pick_cam(scene_key, manifest, ids, *bounds)
    if cam is not None or key_extra and str(key_extra[-1]).startswith("scope:"):
        identity = {**identity, "cam": expected_cam, "window": list(bounds)} if identity else None
    with (trace.traced(run, f"vlm.{task}", {"scene_key": scene_key, "t_ms": t_ms, "cam_id": cam, "actor_ids": ids,
                                            "record_id": rid}, state="inspect") if run is not None else nullcontext()) as st:
        # only C1 looks at a window around t_ms; C2 must see nothing after its own t_ms and C3-C5 are keyed by their window
        cache_source = []
        rec = _cached(scene_key, rid, t_ms, id_at if task == "C1" and not key_extra and cam is None else None,identity,cache_source,
                      validator=lambda r: _validate_record(r, bounds, expected_cam, manifest))
        state = cache_source[0] if cache_source else "precomputed"
        if rec is None:
            try:
                rec, state = _live(task, scene_key, cam, t_ms, actor_refs, ids, window, prompt, schema, make_payload, rid,
                                   region, run, getattr(run, "deadline", None)), "live"
            except VLMUnavailable as exc:
                if st is not None and hasattr(exc, 'raw'):
                    st.output({'validation_error':str(exc),'raw':exc.raw})
                raise
            p = _cache_dir() / f"{rid}.json"
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=p.parent, suffix='.tmp', delete=False) as f:
                    tmp = Path(f.name)
                    try:
                        f.write(json.dumps({"identity":identity,"record":rec.model_dump(mode="json")}).encode())
                        f.close()
                        os.replace(tmp,p)
                    finally:
                        tmp.unlink(missing_ok=True)
            except OSError:
                log.warning("VLM cache write failed for %s", rid, exc_info=True)
        # Runtime-only metadata: not part of the Cosmos schema or persisted payload.
        object.__setattr__(rec, '_sva_exec_state', state)
        if st is not None:
            st.exec_state(state)
            st.output(rec.model_dump())
        return rec


def _cache_dir() -> Path:
    return Path(os.environ.get("RUNS_DIR", "runs")) / "vlm_cache"


@lru_cache(maxsize=1024)
def _material_digest(path, size, mtime_ns, ctime_ns):
    # Metadata invalidates the memoized hash; it is not the material identity.
    import hashlib
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        while chunk := source.read(1024*1024):
            remaining(TIMEOUT_S)
            digest.update(chunk)
    return 'sha256:'+digest.hexdigest()


def _source_identity(scene_key):
    """Hash actual VLM inputs and release metadata; unversioned remote sources miss."""
    import hashlib
    m = json.loads(json.dumps(_manifest(scene_key)))
    m.get('assets',{}).pop('cosmos',None)
    m.get('pipeline',{}).get('models',{}).pop('vlm',None)
    release = os.environ.get('EVIDENCE_REVISION','')
    root = os.environ.get('ASSET_DIR')
    artifacts = []
    if root:
        directory = Path(root)/scene_key
        paths = set()
        for camera in m['cams']:
            media = [camera['video']] if camera.get('video') else [c['uri'] for c in camera.get('chunks',[])]
            for rel in media:
                if rel.startswith(('s3://','https://','http://')):
                    if not release:
                        return None
                else:
                    paths.add(directory/rel)
        assets = m.get('assets',{})
        if assets.get('entities'):
            paths.add(directory/assets['entities'])
        for name in ('boxes_dir','ego_dir'):
            if assets.get(name):
                paths.update((directory/assets[name]).rglob('*.json'))
        for path in sorted(paths):
            remaining(TIMEOUT_S)
            rel = str(path.relative_to(directory))
            if path.is_file():
                stat = path.stat()
                artifacts.append([rel,_material_digest(str(path),stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)])
            else:
                artifacts.append([rel,'missing'])
    elif not release:
        return None
    raw = json.dumps([m,release,artifacts],sort_keys=True,separators=(',',':'))
    return 'sha256:'+hashlib.sha256(raw.encode()).hexdigest()


def _cache_identity(scene_key):
    source = _source_identity(scene_key)
    settings = Settings()
    model = settings.vlm_model
    return {'model':model,'endpoint':settings.vlm_url,'source':source} if source and model else None


def _cached(scene_key: str, rid: str, t_ms: int, id_at, identity=None, source=None, validator=None) -> CosmosRecord | None:
    if identity is None:
        return None
    p = _cache_dir() / f"{rid}.json"
    if p.is_file():
        try:
            cached = json.loads(p.read_text())
            if cached.get('identity')==identity:
                rec = CosmosRecord.model_validate(cached['record'])
                if rec.model_rev==identity['model']:
                    if validator:
                        validator(rec)
                    if source is not None:
                        source.append('cached')
                    return rec
        except (ValueError,KeyError,TypeError,VLMUnavailable):
            pass  # old, incomplete or corrupt caches are misses
    rel = _manifest(scene_key)['assets'].get('cosmos')
    published = _json(scene_key,rel) if rel else None
    # Published results need a positive source match. Missing legacy provenance
    # does not authorize replay against a newer scene or a different model.
    if not published or published.get('input_artifact_hashes',{}).get('scene:source_identity')!=identity['source']:
        return None
    near = [r for r in published['records'] if r.get('model_rev')==identity['model'] and
            (r['record_id']==rid or (id_at and abs(r['t_ms']-t_ms)<=NEAR_MS and r['record_id']==id_at(r['t_ms'])))]
    if near:
        if source is not None:
            source.append('precomputed')
        for row in sorted(near, key=lambda r: abs(r['t_ms']-t_ms)):
            try:
                record = CosmosRecord.model_validate(row)
                if validator:
                    validator(record)
                return record
            except (ValueError, TypeError, VLMUnavailable):
                continue
    return None


def _step(lo: int, hi: int) -> int:
    """Sampling interval of a window: 4 fps unless that would exceed MAX_FRAMES."""
    return max(STEP_MS, -(-(hi - lo) // MAX_FRAMES))


def _live(task, scene_key, cam, t_ms, actor_refs, ids, window, prompt, schema, make_payload, rid, region, run, deadline):
    m = _manifest(scene_key)
    lo, hi = max(window[0], m["t0_ms"]), min(window[1], m["t1_ms"])
    cam = cam or _pick_cam(scene_key, m, ids, lo, hi)
    step = _step(lo, hi)
    frames = [f for f in _frames(scene_key, m, cam, list(range(lo, hi, step)), set(ids), region, hard_window=(lo, hi))
              if lo <= f[0] < hi]
    if not frames:
        raise VLMUnavailable(f"{scene_key}/{cam}: no video frames in [{lo}, {hi})")
    if m["workspace"] == "street":
        head = (f"These are {len(frames)} frames from street camera {cam} sampled at {1000 / step:g} fps from "
                f"{frames[0][0] / 1000:.2f}s to {frames[-1][0] / 1000:.2f}s; each frame shows its time at the bottom; the "
                "time is an overlay, not part of the scene. ")
    else:
        head = (f"These are {len(frames)} frames from {'a dashcam' if _fleet(scene_key) else 'warehouse camera ' + cam} "
                f"sampled at {1000 / step:g} fps from {frames[0][0] / 1000:.2f}s to {frames[-1][0] / 1000:.2f}s; each frame "
                "shows its time at the bottom and tracked objects are boxed with their ids (P = person, F = forklift, "
                "V = vehicle); these boxes, ids and times are overlays, not part of the scene. ")
    out = _chat([_jpeg_b64(img) for _, img in frames], head + prompt + " Answer with JSON only.", schema, run, deadline)
    try:
        payload = make_payload(out, frames)
    except (ValueError, LookupError, TypeError) as e:
        raise VLMUnavailable(f"{task} output lacks a payload field: {type(e).__name__} {e}") from e
    rec = {"record_id": rid, "task": task, "t_ms": t_ms, "cam_id": cam,
           "actor_refs": [a.model_dump() for a in actor_refs],
           "input_frame_refs": [{"cam_id": cam, "t_ms": ms} for ms, _ in frames], "prefix_only": task == "C2",
           "model_rev": Settings().vlm_model, "prompt_rev": _rev(task, scene_key), "payload": payload, "raw": out,
           "verification": {"status": "unverified", "method": "model output, not checked", "ref": None}}
    errs = [e.message for e in _record_validator().iter_errors(rec)]
    if errs:
        raise VLMUnavailable(f"{task} output fails cosmos.json: {errs[0][:200]}")
    record = CosmosRecord.model_validate(rec)
    try:
        _validate_record(record, (lo, hi), cam, m)
    except VLMUnavailable as exc:
        exc.raw = out
        raise
    return record


def _coverage(times: list[int], window: tuple[int, int]) -> list[list[int]]:
    """Contiguous [lo, hi] ms spans actually read: each frame covers one sampling step, never past the window; frames
    more than 1.5 steps apart (a missing sample, beyond the lag of a decoded frame behind its target) leave a gap."""
    step, spans = _step(*window), []
    for t in sorted(times):
        if spans and t - spans[-1][2] <= step * 3 // 2:
            spans[-1][1:] = [min(window[1], t + step), t]
        else:
            spans.append([t, min(window[1], t + step), t])
    return [s[:2] for s in spans]


def _validate_record(record, window, cam, manifest):
    """One validator for decoded, local-cache and published model receipts."""
    errors = [e.message for e in _record_validator().iter_errors(record.model_dump(mode='json'))]
    if errors:
        code = 'invalid_model_localization' if record.task=='C4' else 'invalid_model_grounding' if record.task=='C3' else 'invalid_model_record'
        raise VLMUnavailable(f'{code}: {errors[0][:160]}')
    frames = record.input_frame_refs
    if not frames or record.cam_id != cam or any(f.cam_id != cam or not window[0] <= f.t_ms < window[1] for f in frames):
        raise VLMUnavailable("invalid_model_scope: frame camera/PTS outside requested window")
    covered = lambda a, b: any(lo <= a*1000 and b*1000 <= hi for lo, hi in _coverage([f.t_ms for f in frames], window))  # noqa: E731
    if record.task in ('C1','C2') and isinstance(record.raw,dict):
        intervals=record.raw.get('source_intervals',[])
        if not isinstance(intervals,list):
            raise VLMUnavailable('invalid_model_localization: invalid source intervals')
        for interval in intervals:
            if not isinstance(interval,(list,tuple)) or len(interval)!=2 or not all(type(v) in (int,float) and math.isfinite(v) for v in interval):
                raise VLMUnavailable('invalid_model_localization: invalid source interval')
            a,b=interval
            if a < 0 or b < a or not covered(a,b):
                raise VLMUnavailable('invalid_model_localization: source interval outside read window')
    if record.task == "C4":
        for seg in record.payload.get("segments", []):
            a, b = seg.get("start_s"), seg.get("end_s")
            if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (a, b)) or not 0 <= a <= b or not covered(a, b):
                raise VLMUnavailable("invalid_model_localization: segment outside read window or reversed")
    if record.task == "C3":
        for obj in record.payload.get("objects", []):
            box = obj.get("box_0_1000", [])
            if len(box) != 4 or not all(isinstance(v, (int, float)) and math.isfinite(v) and 0 <= v <= 1000 for v in box) or box[0] >= box[2] or box[1] >= box[3]:
                raise VLMUnavailable("invalid_model_grounding: invalid box")


def _chat(images: list[str], prompt: str, schema: dict, run: Any, deadline: float | None = None) -> dict:
    settings = Settings()
    if run is None:
        fails, opened, circuit = _fails, _opened, (settings.vlm_url, settings.vlm_model)
    else:  # on the run handle, so the state ends with the run
        fails, opened, circuit = vars(run).setdefault('vlm_fails', {}), vars(run).setdefault('vlm_opened', {}), None
    if fails.get(circuit, 0) >= 2:
        if time.monotonic() - opened.get(circuit, 0) < CIRCUIT_COOLDOWN_S:
            raise VLMUnavailable("circuit open after 2 consecutive endpoint failures")
        fails[circuit] = 0
    timeout = TIMEOUT_S if deadline is None else min(TIMEOUT_S, deadline - time.monotonic())  # deadline: time.monotonic() s
    if timeout <= 0:
        raise VLMUnavailable("ask budget exhausted before the VLM call")
    content = [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b}"}} for b in images]
    body = {"model": settings.vlm_model, "temperature": 0, "max_tokens": 1024,
            "messages": [{"role": "user", "content": [*content, {"type": "text", "text": prompt}]}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "out", "schema": schema}}}
    tok = settings.gpu_bearer_token
    try:
        base = settings.vlm_url.rstrip("/")
        if not base:
            raise KeyError("VLM_URL")  # the service root or .../v1, as in llm.complete_json
        r = httpx.post(base + ("" if base.endswith("/v1") else "/v1") + "/chat/completions", json=body, timeout=timeout,
                       headers={"Authorization": f"Bearer {tok}"} if tok else None)
        r.raise_for_status()
    except (httpx.HTTPError, KeyError) as e:
        fails[circuit] = fails.get(circuit, 0) + 1
        if fails[circuit] >= 2:
            opened[circuit] = time.monotonic()
        raise VLMUnavailable(f"VLM request failed: {type(e).__name__} {getattr(getattr(e, 'response', None), 'status_code', '')}") from e
    fails[circuit] = 0
    opened.pop(circuit, None)
    try:
        text = r.json()["choices"][0]["message"]["content"] or ""
    except (ValueError, LookupError, TypeError) as e:
        raise VLMUnavailable("VLM response has no message content") from e
    if not isinstance(text, str):
        raise VLMUnavailable("VLM response content is not text")
    m = re.search(r"\{.*\}", text, re.S)  # the reasoner may wrap the JSON in prose or a code fence
    try:
        parsed = json.loads(m.group(0) if m else text)
        if not isinstance(parsed, dict):
            raise VLMUnavailable("VLM response JSON is not an object")
        return parsed
    except json.JSONDecodeError as e:
        raise VLMUnavailable(f"VLM returned no JSON: {text[:120]!r}") from e


@lru_cache(maxsize=1)
def _record_validator() -> Draft202012Validator:
    doc = json.loads((Path(__file__).resolve().parents[2] / "schemas" / "cosmos.json").read_text())
    return Draft202012Validator(doc["$defs"]["record"])


# --- scene assets ------------------------------------------------------------------------------------------------------

def _asset(scene_key: str, rel: str) -> str:
    """Local path (dev, or site with a mirrored ASSET_DIR) or presigned URL (site) of one scene asset."""
    root = Settings().asset_dir
    if root is not None:
        return str(root / scene_key / rel)
    return _s3().generate_presigned_url("get_object", ExpiresIn=3600,
                                        Params={"Bucket": os.environ["ASSET_BUCKET"], "Key": f"sva/{scene_key}/{rel}"})


@lru_cache(maxsize=1)
def _s3():
    import boto3
    return boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT"], aws_access_key_id=os.environ["ACCESS_KEY"],
                        aws_secret_access_key=os.environ["SECRET_KEY"], verify=Settings().s3_verify)


def _json(scene_key: str, rel: str) -> Any:
    """Parsed JSON asset; None when the file does not exist (a chunk with no rows)."""
    remaining(30)
    src = _asset(scene_key, rel)
    if src.startswith("http"):
        r = httpx.get(src, timeout=remaining(30))
        remaining(30)
        return None if r.status_code == 404 else r.raise_for_status().json()
    return json.loads(Path(src).read_text()) if Path(src).is_file() else None


def _manifest(scene_key: str) -> dict:
    m = _json(scene_key, "manifest.json")
    if m is None:
        raise VLMUnavailable(f"scene {scene_key} not found")
    return m


def _observations(scene_key: str, m: dict) -> list[tuple[str, str, int, int, int]]:
    """(entity_id, cam_id, local_track_id, t0_ms, t1_ms) for every observation in entities.json, clipped to the time
    its association is valid."""
    ents = _json(scene_key, m["assets"]["entities"]) if "entities" in m["assets"] else None
    return [(e["entity_id"], o["cam_id"], o["local_track_id"], t0, t1)
            for e in (ents or {"entities": []})["entities"] for o in e["observations"]
            for t0, t1 in [(max(o["t0_ms"], e["association"]["valid"][0]), min(o["t1_ms"], e["association"]["valid"][1]))]
            if t0 < t1]


def _pick_cam(scene_key: str, m: dict, ids: list[str], lo: int, hi: int) -> str:
    """The camera that observes the actors longest inside [lo, hi); manifest order breaks ties."""
    seen = {c["id"]: 0 for c in m["cams"]}
    for eid, cam, _, t0, t1 in _observations(scene_key, m):
        if eid in ids and cam in seen:
            seen[cam] += max(0, min(t1, hi) - max(t0, lo))
    return max(seen, key=lambda c: seen[c])


def _boxes(scene_key: str, m: dict, cam: str, lo: int, hi: int) -> dict[int, list]:
    """local_track_id -> rows [t_ms, ltid, cls, x1, y1, x2, y2, conf] of this camera inside [lo, hi]."""
    out: dict[int, list] = {}
    for chunk in range(lo // 10000, hi // 10000 + 1):
        doc = _json(scene_key, f"{m['assets'].get('boxes_dir', 'boxes')}/{cam}/{chunk:05d}.json")
        for row in (doc or {"rows": []})["rows"]:
            if lo - BOX_MATCH_MS <= row[0] <= hi + BOX_MATCH_MS:
                out.setdefault(row[1], []).append(row)
    return out


def _bands(scene_key: str, m: dict, bid: str, lo: int, hi: int) -> list[tuple[int, float, float]]:
    """(t_ms, u0, u1) source-pixel columns of building `bid` in the fleet ego frames inside [lo, hi]."""
    out = []
    for chunk in range(lo // 10000, hi // 10000 + 1):
        doc = _json(scene_key, f"{m['assets'].get('ego_dir', 'ego')}/{chunk:05d}.json")
        out += [(f["t_ms"], b["u0"], b["u1"]) for f in (doc or {"frames": []})["frames"] if lo <= f["t_ms"] <= hi
                for b in f["buildings"] if b["bid"] == bid]
    return out


def _frames(scene_key: str, m: dict, cam: str, targets: list[int], actors: set[str],
            region: str | None, *, hard_window: tuple[int, int] | None = None) -> list[tuple[int, Image.Image]]:
    """(scene_ms, annotated frame) for the first decoded frame at or after each target time; targets without video are
    skipped. Frames get the SoM boxes of every tracked entity (actors drawn thicker), the column band of a building
    region, and the scene time at the bottom."""
    c = next(x for x in m["cams"] if x["id"] == cam)
    if not targets:
        return []
    street = m["workspace"] == "street"
    # street cameras have no tracks, entities or ego frames to draw
    rows = {} if street else _boxes(scene_key, m, cam, targets[0], targets[-1] + STEP_MS)
    obs = [] if street else [o for o in _observations(scene_key, m) if o[1] == cam]
    bands = _bands(scene_key, m, region, targets[0], targets[-1] + STEP_MS) if region and not ENTITY_ID_RE.match(region) and not street else []
    if c["video"]:
        sources = [(_asset(scene_key, c["video"]), c["offset_ms"])]
    else:  # a site street camera: its VSS chunks, each starting at its own scene time
        sources = [(_chunk(scene_key, ch["uri"]), c["offset_ms"] + ch["t0_ms"]) for ch in c["chunks"]
                   if ch["t0_ms"] <= targets[-1] and ch["t1_ms"] > targets[0]]
    out, i = [], 0
    for src, offset in sources:
        if i == len(targets):
            break
        remaining(TIMEOUT_S)
        # Download remote media with bounded HTTP I/O. Native VideoCapture URL
        # reads otherwise bypass httpx and the investigation deadline entirely.
        temporary = None
        if src.startswith(('https://','http://')):
            handle = tempfile.NamedTemporaryFile(suffix='.mp4',delete=False)
            temporary = Path(handle.name)
            try:
                with handle, httpx.stream('GET',src,timeout=remaining(30)) as response:
                    response.raise_for_status()
                    for chunk in response.iter_bytes():
                        remaining(30)
                        handle.write(chunk)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
            src = str(temporary)
        cap = cv2.VideoCapture(src)
        cap.set(cv2.CAP_PROP_POS_MSEC, max(0, targets[i] - offset - 500))
        try:
            while i < len(targets):
                remaining(TIMEOUT_S)
                ok, bgr = cap.read()
                remaining(TIMEOUT_S)
                if not ok:
                    break
                ms = round(cap.get(cv2.CAP_PROP_POS_MSEC)) + offset
                if ms < targets[i]:
                    continue
                while i < len(targets) and targets[i] <= ms:
                    i += 1
                if ms - targets[i - 1] < STEP_MS:  # a gap in the video skips the target rather than shifting it
                    out.append((ms, _annotate(bgr, ms, c, rows, obs, actors, [(region, b) for b in bands],
                                             window=hard_window, max_age_ms=_sample_age(m) if hard_window else BOX_MATCH_MS)))
        finally:
            cap.release()
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return out


def _chunk(scene_key: str, uri: str) -> str:
    """A street chunk: the scene asset in fixtures; on site the copy street.local_chunk keeps under RUNS_DIR, else a
    presigned URL of the VSS object."""
    if not uri.startswith("s3://"):
        return _asset(scene_key, uri)
    cached = Path(os.environ.get("RUNS_DIR", "runs")) / "street" / "chunks" / (street_digest(uri+('|' + os.environ['EVIDENCE_REVISION'] if os.environ.get('EVIDENCE_REVISION') else '')) + ".mp4")
    if cached.is_file():
        return str(cached)
    bucket, key = uri[5:].split("/", 1)
    return _s3().generate_presigned_url("get_object", ExpiresIn=3600, Params={"Bucket": bucket, "Key": key})


def _sample_age(manifest: dict) -> float:
    fps = manifest.get('fps')
    return min(BOX_MATCH_MS, 500 / fps) if type(fps) in (int, float) and math.isfinite(fps) and fps > 0 else 0


def _frame_bindings(ms: int, cam: dict, rows: dict[int, list], obs: list,
                    *, window=None, max_age_ms=BOX_MATCH_MS) -> dict:
    """Only unique, current entity-to-box ownership may produce a SoM label."""
    bindings = {}
    for ltid, samples in rows.items():
        owners = [o for o in obs if o[1] == cam['id'] and o[2] == ltid and o[3] <= ms < o[4]]
        if len(owners) != 1:
            continue
        owner = owners[0]
        legal = []
        for row in samples:
            if len(row) != 8 or row[1] != ltid:
                continue
            t = row[0]
            if type(t) not in (int, float) or not math.isfinite(t) or abs(t-ms) > max_age_ms:
                continue
            if window is not None and not window[0] <= t < window[1]:
                continue
            sampled_owners = [o for o in obs if o[1] == cam['id'] and o[2] == ltid and o[3] <= t < o[4]]
            if len(sampled_owners) != 1 or sampled_owners[0] != owner:
                continue
            if any(type(v) not in (int,float) or not math.isfinite(v) for v in row[3:8]):
                continue
            x1,y1,x2,y2,conf = row[3:8]
            if not (0 <= x1 < x2 <= cam['w'] and 0 <= y1 < y2 <= cam['h'] and 0 <= conf <= 1):
                continue
            legal.append(row)
        if legal:
            # Conflicting equally close boxes are not a unique visual binding.
            distance = min(abs(row[0]-ms) for row in legal)
            nearest = [row for row in legal if abs(row[0]-ms) == distance]
            if len({tuple(row) for row in nearest}) == 1:
                bindings[ltid] = (owner[0], nearest[0])
    # The inverse mapping must also be unique: a requested entity cannot name
    # two simultaneous local tracks in one camera frame.
    entity_counts = {}
    for eid,_ in bindings.values():
        entity_counts[eid] = entity_counts.get(eid,0)+1
    return {ltid:binding for ltid,binding in bindings.items() if entity_counts[binding[0]] == 1}


def _annotate(bgr, ms: int, cam: dict, rows: dict[int, list], obs: list, actors: set[str], bands: list,
              *, window=None, max_age_ms=BOX_MATCH_MS) -> Image.Image:
    im = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
    im = im.resize((FRAME_W, round(im.height * FRAME_W / im.width)))
    s = FRAME_W / cam["w"]  # boxes are in source pixels
    d = ImageDraw.Draw(im)
    font = ImageFont.load_default(size=16)
    near = [b for b in bands if abs(b[1][0] - ms) <= BOX_MATCH_MS]
    if near:
        bid, (_, u0, u1) = min(near, key=lambda b: abs(b[1][0] - ms))
        d.rectangle((u0 * s, 0, u1 * s, im.height - 1), outline=(0, 200, 255), width=3)
        d.text((u0 * s + 4, 4), bid, fill=(0, 200, 255), font=font, stroke_width=2, stroke_fill=(0, 0, 0))
    for eid, r in _frame_bindings(ms, cam, rows, obs, window=window, max_age_ms=max_age_ms).values():
        x1, y1, x2, y2 = (v * s for v in r[3:7])
        d.rectangle((x1, y1, x2, y2), outline=(255, 220, 0), width=3 if eid in actors else 1)
        d.text((x1 + 2, max(0, y1 - 18)), eid, fill=(255, 220, 0), font=font, stroke_width=2, stroke_fill=(0, 0, 0))
    label = f"t={ms / 1000:.2f}s"
    w = d.textlength(label, font=font)
    d.rectangle((0, im.height - 22, w + 12, im.height), fill=(0, 0, 0))
    d.text((6, im.height - 20), label, fill=(255, 255, 255), font=font)
    return im


def _jpeg_b64(im: Image.Image) -> str:
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


@_budgeted
def observe_window(scene_key: str, cam: str, t_range: tuple[int, int] | list[int],
                   actor_refs: list[EntityRef], *, run: Any = None) -> dict:
    """Bounded visual observations with server-resolved frame citations.

    This is a separate observation contract, not a spatial measurement or a C1
    event label. Decode on replay too, so cache provenance covers actual pixels.
    """
    import hashlib
    lo, hi = t_range
    if not isinstance(lo,int) or not isinstance(hi,int) or lo < 0 or hi <= lo:
        raise VLMUnavailable('invalid_model_scope')
    manifest = _manifest(scene_key)
    if not any(c['id']==cam for c in manifest['cams']):
        raise VLMUnavailable('invalid_model_scope: unknown camera')
    lo,hi = max(lo,manifest['t0_ms']),min(hi,manifest['t1_ms'])
    if hi <= lo:
        raise VLMUnavailable('invalid_model_scope: empty window')
    n = min(MAX_FRAMES,max(1,(hi-lo+STEP_MS-1)//STEP_MS))
    targets = sorted({lo+(hi-lo-1)*i//max(1,n-1) for i in range(n)})
    ids = sorted({a.entity_id for a in actor_refs})
    frames = [f for f in _frames(scene_key,manifest,cam,targets,set(ids),None,hard_window=(lo,hi)) if lo <= f[0] < hi][:MAX_FRAMES]
    if not frames:
        raise VLMUnavailable(f'{scene_key}/{cam}: no video frames in [{lo}, {hi})')
    frame_refs = [{'cam_id':cam,'t_ms':ms} for ms,_ in frames]
    settings = Settings()
    revision = 'observation-3'
    camera = next(c for c in manifest['cams'] if c['id'] == cam)
    boxes = _boxes(scene_key,manifest,cam,lo,hi) if manifest['workspace'] != 'street' else {}
    observations = _observations(scene_key,manifest) if manifest['workspace'] != 'street' else []
    bound_ids = [sorted({eid for eid,_ in _frame_bindings(ms,camera,boxes,observations,
                    window=(lo,hi),max_age_ms=_sample_age(manifest)).values()} & set(ids)) for ms,_ in frames]
    image_hashes = [hashlib.sha256(img.tobytes()+str((img.mode,img.size)).encode()).hexdigest() for _,img in frames]
    identity = {'scene':scene_key,'cam':cam,'window':[lo,hi],'actors':ids,'frames':frame_refs,
                'pixels':image_hashes,'bound_subjects':bound_ids,'material':_cache_identity(scene_key),'model':settings.vlm_model,
                'endpoint':settings.vlm_url,'prompt':revision}
    digest = hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    cache = _cache_dir() / ('observation_'+digest+'.json')
    from app.core.model_observation import quantified  # model_observation imports this module
    def validate(raw):
        if not isinstance(raw,dict) or set(raw)!= {'facts'} or not isinstance(raw['facts'],list) or len(raw['facts'])>6:
            raise VLMUnavailable('invalid_model_observation: expected at most six facts')
        facts=[]
        for item in raw['facts']:
            if not isinstance(item,dict) or set(item)!={'subject_ids','text','frame_indices','uncertainty'}:
                raise VLMUnavailable('invalid_model_observation: invalid fact fields')
            subjects,text,indices,u = (item[k] for k in ('subject_ids','text','frame_indices','uncertainty'))
            if not isinstance(subjects,list) or any(v not in ids for v in subjects):
                raise VLMUnavailable('invalid_model_observation: unknown subject')
            if not isinstance(text,str) or not text.strip() or len(text)>300:
                raise VLMUnavailable('invalid_model_observation: invalid text')
            if not isinstance(indices,list) or not indices or any(type(v) is not int or not 0 <= v < len(frames) for v in indices):
                raise VLMUnavailable('invalid_model_observation: invalid frame index')
            if any(subject not in bound_ids[i] for subject in subjects for i in indices):
                raise VLMUnavailable('invalid_model_observation: subject lacks a unique current visual binding')
            if any(subject not in subjects for subject in re.findall(r'\b[PFV]\d+\b',text)):
                raise VLMUnavailable('invalid_model_observation: text subject is not bound')
            if type(u) not in (int,float) or not math.isfinite(u) or not 0 <= u <= 1:
                raise VLMUnavailable('invalid_model_observation: invalid uncertainty')
            # The model may describe visible motion, never smuggle metric claims
            # into this nonmeasurement channel.
            if quantified(text,ids):
                raise VLMUnavailable('invalid_model_observation: numeric measurement in visual text')
            facts.append({**item,'frame_refs':[frame_refs[i] for i in dict.fromkeys(indices)]})
        return facts
    raw = None
    state = 'live'
    if cache.is_file():
        try:
            saved=json.loads(cache.read_text())
            if saved['identity']==identity:
                validate(saved['raw'])
                raw=saved['raw'];state='cached'
        except (ValueError,KeyError,TypeError,VLMUnavailable):
            pass
    if raw is None:
        images=[]
        for i,(_,image) in enumerate(frames):
            image=image.copy()
            ImageDraw.Draw(image).text((5,25),f'frame_index={i}',fill='white',stroke_width=2,stroke_fill='black')
            images.append(_jpeg_b64(image))
        schema=_obj({'facts':{'type':'array','maxItems':6,'items':_obj({
            'subject_ids':{'type':'array','items':{'type':'string'},'maxItems':len(ids)},
            'text':{'type':'string','maxLength':300},
            'frame_indices':{'type':'array','items':{'type':'integer','minimum':0,'maximum':len(frames)-1},'minItems':1,'maxItems':MAX_FRAMES},
            'uncertainty':{'type':'number','minimum':0,'maximum':1}})}})
        prompt=(f'Observe only the visible state, motion and interaction in these {len(frames)} frames from camera {cam}. '
                f'Requested tracked subjects are {ids}. The visible bound subjects per frame_index are {bound_ids}. '
                'A subject_ids reference must be bound in every cited frame. Never name an unbound subject in text. '
                'Use an empty subject_ids list and anonymous descriptions for scene observations or unseen requested actors. '
                'Provide at most six atomic facts, each citing frame_index integers printed on actual input frames. '
                'Describe what is visible, not inferred intent, blame, hidden objects, numeric distance, count, speed or time. Use non-quantified singular or plural descriptions; do not spell out counts such as one person, two vehicles, several people, or many cars. '
                'An overlay ID is a reference, not proof of real-world identity. Empty facts is valid when evidence is insufficient. '
                'uncertainty is zero for confident and one for uncertain. Answer JSON only.')
        raw=_chat(images,prompt,schema,run,getattr(run,'deadline',None))
        facts=validate(raw)
        try:
            cache.parent.mkdir(parents=True,exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=cache.parent,delete=False) as handle:
                temporary=Path(handle.name)
                handle.write(json.dumps({'identity':identity,'raw':raw}).encode())
            try:temporary.replace(cache)
            finally:temporary.unlink(missing_ok=True)
        except OSError:
            log.warning('VLM observation cache write failed for %s',digest,exc_info=True)
    else:
        facts=validate(raw)
    return {'raw':raw,'facts':facts,'cam_id':cam,'t0_ms':lo,'t1_ms':hi,
            'input_frame_refs':frame_refs,'model_rev':settings.vlm_model,'prompt_rev':revision,
            'exec_state':state,'input_digest':'sha256:'+digest}
