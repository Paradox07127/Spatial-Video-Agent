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
import os
import re
import time
from contextlib import nullcontext
from functools import lru_cache
from pathlib import Path
from typing import Any

import cv2
import httpx
from jsonschema import Draft202012Validator
from PIL import Image, ImageDraw, ImageFont

from app.core import trace
from app.core.models import ENTITY_ID_RE, CosmosRecord, EntityRef, make_record_id
from app.core.street import _digest as street_digest

# prompt revision per task, part of every record_id: bump only the prompt that changed so other records stay found
PROMPT_REV = {"C1": "px-2", "C1-fleet": "px-1", "C2": "px-1", "C3": "px-1", "C4": "px-1", "C5": "px-1",
              "C3-street": "st-1", "C4-street": "st-1"}
STEP_MS = 250  # 4 fps, the rate Cosmos temporal prompts expect
WINDOW_MS = 3000
PREFIX_MS = 2000
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
_fails: dict[str | None, int] = {}  # consecutive endpoint failures per run_id (None = calls without a run)


class VLMUnavailable(RuntimeError):
    """Endpoint down, circuit open (2 consecutive 502/timeouts), or no cached record in cache-only mode."""


def situation(scene_key: str, t_ms: int, actor_refs: list[EntityRef], *, run: Any = None) -> CosmosRecord:
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
    return _call("C1", scene_key, None, t_ms, actor_refs, (t_ms - WINDOW_MS, t_ms + WINDOW_MS), [], prompt, schema,
                 lambda o, f: {"label": o["label"], "reason": o["reason"], "uncertainty": o["uncertainty"]}, run)


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


def localize(scene_key: str, cam: str, t0_ms: int, t1_ms: int, query: str, *, run: Any = None) -> CosmosRecord:
    """C4: segments [{start_s, end_s, text}] in scene seconds."""
    prompt = (f"Question: {query}? Give each time segment where it happens as start_s and end_s, read from the timestamps "
              "printed on the frames, with a few words of text. Return an empty list if it does not happen in these frames.")
    schema = _obj({"segments": {"type": "array", "items": _obj(
        {"start_s": {"type": "number"}, "end_s": {"type": "number"}, "text": {"type": "string"}})}})
    return _call("C4", scene_key, cam, (t0_ms + t1_ms) // 2, [], (t0_ms, t1_ms), [query, f"{t0_ms}-{t1_ms}"], prompt,
                 schema, lambda o, f: {"query": query, "segments": o["segments"]}, run)


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


def _call(task, scene_key, cam, t_ms, actor_refs, window, key_extra, prompt, schema, make_payload, run, region=None):
    ids = [a.entity_id for a in actor_refs] + ([region] if region and ENTITY_ID_RE.match(region) else [])
    t_ms = int(t_ms)
    def id_at(t):  # key_extra: query/vocab/region/horizon
        return make_record_id(scene_key, task, t, cam, ids + key_extra, _rev(task, scene_key))
    rid = id_at(t_ms)
    with (trace.traced(run, f"vlm.{task}", {"scene_key": scene_key, "t_ms": t_ms, "cam_id": cam, "actor_ids": ids,
                                            "record_id": rid}, state="inspect") if run is not None else nullcontext()) as st:
        # only C1 looks at a window around t_ms; C2 must see nothing after its own t_ms and C3-C5 are keyed by their window
        rec, state = _cached(scene_key, rid, t_ms, id_at if task == "C1" else None), "cached"
        if rec is None:
            rec, state = _live(task, scene_key, cam, t_ms, actor_refs, ids, window, prompt, schema, make_payload, rid,
                               region, getattr(run, "run_id", None), getattr(run, "deadline", None)), "live"
            p = _cache_dir() / f"{rid}.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(rec.model_dump_json())
        if st is not None:
            st.exec_state(state)
            st.output(rec.model_dump())
        return rec


def _cache_dir() -> Path:
    return Path(os.environ.get("RUNS_DIR", "runs")) / "vlm_cache"


def _cached(scene_key: str, rid: str, t_ms: int, id_at) -> CosmosRecord | None:
    """Disk cache first, then the scene's pre-run cosmos/<scene_key>.json (cosmos_batch): the same request at its own
    t_ms or, failing that, at the nearest pre-run moment within NEAR_MS. id_at(t) = the record id of this request at t;
    None = exact id only."""
    p = _cache_dir() / f"{rid}.json"
    if p.is_file():
        return CosmosRecord.model_validate_json(p.read_text())
    rel = _manifest(scene_key)["assets"].get("cosmos")
    near = [r for r in (_json(scene_key, rel)["records"] if rel else [])
            if r["record_id"] == rid or (id_at and abs(r["t_ms"] - t_ms) <= NEAR_MS and r["record_id"] == id_at(r["t_ms"]))]
    return CosmosRecord.model_validate(min(near, key=lambda r: abs(r["t_ms"] - t_ms))) if near else None


def _live(task, scene_key, cam, t_ms, actor_refs, ids, window, prompt, schema, make_payload, rid, region, run_id, deadline):
    m = _manifest(scene_key)
    lo, hi = max(window[0], m["t0_ms"]), min(window[1], m["t1_ms"])
    cam = cam or _pick_cam(scene_key, m, ids, lo, hi)
    frames = [f for f in _frames(scene_key, m, cam, list(range(lo, hi, STEP_MS)), set(ids), region) if f[0] < hi]
    if not frames:
        raise VLMUnavailable(f"{scene_key}/{cam}: no video frames in [{lo}, {hi})")
    if m["workspace"] == "street":
        head = (f"These are {len(frames)} frames from street camera {cam} sampled at 4 fps from {frames[0][0] / 1000:.2f}s "
                f"to {frames[-1][0] / 1000:.2f}s; each frame shows its time at the bottom; the time is an overlay, not part "
                "of the scene. ")
    else:
        head = (f"These are {len(frames)} frames from {'a dashcam' if _fleet(scene_key) else 'warehouse camera ' + cam} "
                f"sampled at 4 fps from {frames[0][0] / 1000:.2f}s to {frames[-1][0] / 1000:.2f}s; each frame shows its time "
                "at the bottom and tracked objects are boxed with their ids (P = person, F = forklift, V = vehicle); "
                "these boxes, ids and times are overlays, not part of the scene. ")
    out = _chat([_jpeg_b64(img) for _, img in frames], head + prompt + " Answer with JSON only.", schema, run_id, deadline)
    payload = make_payload(out, frames)
    rec = {"record_id": rid, "task": task, "t_ms": t_ms, "cam_id": cam,
           "actor_refs": [a.model_dump() for a in actor_refs],
           "input_frame_refs": [{"cam_id": cam, "t_ms": ms} for ms, _ in frames], "prefix_only": task == "C2",
           "model_rev": os.environ.get("VLM_MODEL", ""), "prompt_rev": _rev(task, scene_key), "payload": payload, "raw": out,
           "verification": {"status": "unverified", "method": "model output, not checked", "ref": None}}
    errs = [e.message for e in _record_validator().iter_errors(rec)]
    if errs:
        raise VLMUnavailable(f"{task} output fails cosmos.json: {errs[0][:200]}")
    return CosmosRecord.model_validate(rec)


def _chat(images: list[str], prompt: str, schema: dict, run_id: str | None, deadline: float | None = None) -> dict:
    if _fails.get(run_id, 0) >= 2:
        raise VLMUnavailable("circuit open after 2 consecutive endpoint failures")
    timeout = TIMEOUT_S if deadline is None else min(TIMEOUT_S, deadline - time.monotonic())  # deadline: time.monotonic() s
    if timeout <= 0:
        raise VLMUnavailable("ask budget exhausted before the VLM call")
    content = [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b}"}} for b in images]
    body = {"model": os.environ.get("VLM_MODEL", ""), "temperature": 0, "max_tokens": 1024,
            "messages": [{"role": "user", "content": [*content, {"type": "text", "text": prompt}]}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "out", "schema": schema}}}
    tok = os.environ.get("GPU_BEARER_TOKEN")
    try:
        r = httpx.post(os.environ["VLM_URL"].rstrip("/") + "/chat/completions", json=body, timeout=timeout,
                       headers={"Authorization": f"Bearer {tok}"} if tok else None)
        r.raise_for_status()
    except (httpx.HTTPError, KeyError) as e:
        _fails[run_id] = _fails.get(run_id, 0) + 1
        raise VLMUnavailable(f"VLM request failed: {type(e).__name__} {getattr(getattr(e, 'response', None), 'status_code', '')}") from e
    _fails[run_id] = 0
    text = r.json()["choices"][0]["message"]["content"] or ""
    m = re.search(r"\{.*\}", text, re.S)  # the reasoner may wrap the JSON in prose or a code fence
    try:
        return json.loads(m.group(0) if m else text)
    except json.JSONDecodeError as e:
        raise VLMUnavailable(f"VLM returned no JSON: {text[:120]!r}") from e


@lru_cache(maxsize=1)
def _record_validator() -> Draft202012Validator:
    doc = json.loads((Path(__file__).resolve().parents[2] / "schemas" / "cosmos.json").read_text())
    return Draft202012Validator(doc["$defs"]["record"])


# --- scene assets ------------------------------------------------------------------------------------------------------

def _asset(scene_key: str, rel: str) -> str:
    """Local path (dev, or site with a mirrored ASSET_DIR) or presigned URL (site) of one scene asset."""
    if os.environ.get("MODE", "dev") != "site" or os.environ.get("ASSET_DIR"):
        return str(Path(os.environ["ASSET_DIR"]) / scene_key / rel)
    return _s3().generate_presigned_url("get_object", ExpiresIn=3600,
                                        Params={"Bucket": os.environ["ASSET_BUCKET"], "Key": f"sva/{scene_key}/{rel}"})


@lru_cache(maxsize=1)
def _s3():
    import boto3
    return boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT"], aws_access_key_id=os.environ["ACCESS_KEY"],
                        aws_secret_access_key=os.environ["SECRET_KEY"], verify=os.environ.get("S3_VERIFY", "1") != "0")


def _json(scene_key: str, rel: str) -> Any:
    """Parsed JSON asset; None when the file does not exist (a chunk with no rows)."""
    src = _asset(scene_key, rel)
    if src.startswith("http"):
        r = httpx.get(src, timeout=30)
        return None if r.status_code == 404 else r.raise_for_status().json()
    return json.loads(Path(src).read_text()) if Path(src).is_file() else None


def _manifest(scene_key: str) -> dict:
    m = _json(scene_key, "manifest.json")
    if m is None:
        raise VLMUnavailable(f"scene {scene_key} not found")
    return m


def _observations(scene_key: str, m: dict) -> list[tuple[str, str, int, int, int]]:
    """(entity_id, cam_id, local_track_id, t0_ms, t1_ms) for every observation in entities.json."""
    ents = _json(scene_key, m["assets"]["entities"]) if "entities" in m["assets"] else None
    return [(e["entity_id"], o["cam_id"], o["local_track_id"], o["t0_ms"], o["t1_ms"])
            for e in (ents or {"entities": []})["entities"] for o in e["observations"]]


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
            region: str | None) -> list[tuple[int, Image.Image]]:
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
        cap = cv2.VideoCapture(src)
        cap.set(cv2.CAP_PROP_POS_MSEC, max(0, targets[i] - offset - 500))
        while i < len(targets):
            ok, bgr = cap.read()
            if not ok:
                break
            ms = round(cap.get(cv2.CAP_PROP_POS_MSEC)) + offset
            if ms < targets[i]:
                continue
            while i < len(targets) and targets[i] <= ms:
                i += 1
            if ms - targets[i - 1] < STEP_MS:  # a gap in the video skips the target rather than shifting it
                out.append((ms, _annotate(bgr, ms, c, rows, obs, actors, [(region, b) for b in bands])))
        cap.release()
    return out


def _chunk(scene_key: str, uri: str) -> str:
    """A street chunk: the scene asset in fixtures; on site the copy street.local_chunk keeps under RUNS_DIR, else a
    presigned URL of the VSS object."""
    if not uri.startswith("s3://"):
        return _asset(scene_key, uri)
    cached = Path(os.environ.get("RUNS_DIR", "runs")) / "street" / "chunks" / (street_digest(uri) + ".mp4")
    if cached.is_file():
        return str(cached)
    bucket, key = uri[5:].split("/", 1)
    return _s3().generate_presigned_url("get_object", ExpiresIn=3600, Params={"Bucket": bucket, "Key": key})


def _annotate(bgr, ms: int, cam: dict, rows: dict[int, list], obs: list, actors: set[str], bands: list) -> Image.Image:
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
    for ltid, rs in rows.items():
        r = min(rs, key=lambda r: abs(r[0] - ms))
        eid = next((o[0] for o in obs if o[2] == ltid and o[3] <= ms <= o[4]), None)
        if eid is None or abs(r[0] - ms) > BOX_MATCH_MS:
            continue
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
