"""P11: pre-run Cosmos3 C1-C5 on scene moments and stage cosmos/<scene_key>.json for publish.

  python pipeline/common/cosmos_batch.py --workspace warehouse|fleet [--raw] [--limit N]

warehouse: published SDG scenes; moments = events.json near_miss / dodge plus one far moment (the P11b passing contrast);
  per moment C1 (worker, forklift) and C2 (forklift), per scene C3 (occluders), C4 (fixed Dodge query over the run) and
  C5 (forklift). fleet: >= 12 PIE pedestrian moments (crossing, or not crossing as contrast) where the ego car slows
  most, max 3 per video; per moment C1 (pedestrian) and C2 (ego reaction).
--raw also covers scenes that are not published yet: each is staged from raw data (raw video, GT boxes as SoM ids) under
STAGE_ROOT so the prompts can run; its records wait in _tmp/P-X/<scene_key>/ and are published once the scene exists.
Truth (C1 rule label, C2 actual actions, C4 first Dodge frame) comes from SDG GT / PIE annotations; it is written into
each record's verification and into fixtures/labels/<workspace>_rules.json for the eval scripts.
Every request to :8101 holds a lease.vlm_slot; per-request elapsed_ms goes to _logs/P-X/elapsed_<workspace>.json.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np
from shapely.geometry import MultiPoint

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.core import vlm  # noqa: E402
from app.core.models import CosmosVerification, EntityRef  # noqa: E402
from pipeline.common import lease, paths, publish  # noqa: E402
from pipeline.warehouse import sdg  # noqa: E402

CARD = "P-X"
OUT = publish.OUT_ROOT
LOGS = OUT / "_logs" / CARD
STAGE_ROOT = Path.home() / "sva-wt" / CARD / "raw_scenes"
PIE = paths.DATA / "raw" / "pie"
LABELS = Path(__file__).resolve().parents[2] / "fixtures" / "labels"
FPS = 30  # SDG and PIE frame rate; t_ms = round(frame * 1000 / FPS)
C3_VOCAB = ["shelf", "rack", "pallet", "box stack", "door"]
C4_QUERY = "when does the worker start yielding"
WH_RULES = {"stop": 0.3, "slow": -0.5}  # m/s (situation_map C2)
FL_RULES = {"stop": 3.0, "slow": -3.0}  # km/h, PIE OBD
TURN_DEG = 15
DEPTH_A, DEPTH_B = 168.05, 36.27  # SDG depth.mp4: range_m = exp((A - gray) / B)
DEPTH_TAU_M = 0.25  # a box surface counts as real when it sits this far in front of the floor along the ray
WORKER_MATCH_M = 1.5  # max distance between a person root and the GT worker for the person to stand for the worker
ROOT_DT_MS = 100  # a track sample further than this from the moment does not place the person at it
ELAPSED: list[int] = []


def t_of(frame: int) -> int:
    return round(frame * 1000 / FPS)


def ms_frame(t_ms: int) -> int:
    return round(t_ms * FPS / 1000)


def sha(p: Path) -> str:
    return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()


def commit() -> str:
    r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=Path(__file__).parent, capture_output=True, text=True)
    return r.stdout.strip() or "worktree"


# --- SDG ground truth --------------------------------------------------------------------------------------------------

def _footprint(b: dict):
    T = np.array(b["transform"])
    pts = np.array([[x, y, b["z_min"], 1.0] for x in (b["x_min"], b["x_max"]) for y in (b["y_min"], b["y_max"])]) @ T
    return MultiPoint([tuple(p[:2]) for p in pts]).convex_hull


def _bottom_xy(b: dict) -> tuple[float, float]:
    T = np.array(b["transform"])
    c = np.array([(b["x_min"] + b["x_max"]) / 2, (b["y_min"] + b["y_max"]) / 2, b["z_min"], 1.0]) @ T
    return c[0], c[1]


def sdg_gt(run_dir: Path) -> dict:
    """Per frame of ceiling_00.object_detection.jsonl: worker task and bbox3d bottom centre (wxy), worker-forklift footprint
    gap (m), forklift bbox3d bottom centre, heading (local +x, rad) and speed (m/s, central difference over 10 frames).
    NaN where an agent is missing."""
    task, dist, fxy, fh, wxy = [], [], [], [], []
    for line in open(run_dir / "ceiling_00.object_detection.jsonl"):
        ags = {a["label"]["class"]: a for a in json.loads(line)["agents"].values()}
        w, f = ags.get("character"), ags.get("robot")
        task.append(w["metro_agent_data"]["current_task_name"] if w else None)
        wxy.append(_bottom_xy(w["annotators"]["bounding_box_3d_fast"]) if w else (np.nan, np.nan))
        if f is None:
            dist.append(np.nan), fxy.append((np.nan, np.nan)), fh.append(np.nan)
            continue
        b = f["annotators"]["bounding_box_3d_fast"]
        T = np.array(b["transform"])
        c = np.array([(b["x_min"] + b["x_max"]) / 2, (b["y_min"] + b["y_max"]) / 2, b["z_min"], 1.0]) @ T
        ax = np.array([1.0, 0, 0, 0]) @ T
        fxy.append((c[0], c[1])), fh.append(math.atan2(ax[1], ax[0]))
        dist.append(_footprint(w["annotators"]["bounding_box_3d_fast"]).distance(_footprint(b)) if w else np.nan)
    fxy = np.array(fxy)
    n = len(task)
    i0, i1 = np.clip(np.arange(n) - 5, 0, n - 1), np.clip(np.arange(n) + 5, 0, n - 1)
    speed = np.linalg.norm(fxy[i1] - fxy[i0], axis=1) / np.maximum(i1 - i0, 1) * FPS
    return {"t_ms": np.array([t_of(i) for i in range(n)]), "task": task, "dist": np.array(dist), "xy": fxy,
            "heading": np.unwrap(np.nan_to_num(np.array(fh))), "speed": speed, "wxy": np.array(wxy)}


def _at(gt: dict, key: str, t_ms: int):
    return gt[key][min(ms_frame(t_ms), len(gt["task"]) - 1)]


def c1_wh(gt: dict, t_ms: int) -> tuple[str, str]:
    """situation_map C1_warehouse rules at t (Dodge looked up in the +-3 s window the model sees)."""
    lo, hi = ms_frame(t_ms - vlm.WINDOW_MS), ms_frame(t_ms + vlm.WINDOW_MS)
    if "Dodge" in gt["task"][max(lo, 0):hi]:
        return "yielding", "wh_dodge"
    d, v = _at(gt, "dist", t_ms), _at(gt, "speed", t_ms)
    if np.isnan(d):
        return "unrelated", "wh_unrelated"
    if v >= 0.3 and d < 2:
        return "interaction", "wh_interaction"
    if d >= 2:
        return "passing", "wh_passing"
    return "unrelated", "wh_unrelated"


def c2_truth(v0: float, v2: float, h0_deg: float, h2_deg: float, rules: dict) -> list[str]:
    """situation_map C2 rules over [t, t + 2 s]; several labels may hold."""
    acts = (["stop"] if v2 < rules["stop"] else []) + (["slow"] if v2 - v0 <= rules["slow"] else [])
    acts += ["turn"] if abs((h2_deg - h0_deg + 180) % 360 - 180) >= TURN_DEG else []
    return acts or ["continue"]


def c4_wh(gt: dict) -> tuple[str, list[int] | None]:
    """(label, [start_ms, end_ms] of the first Dodge run): label = first Dodge second, 'abstain' or 'boundary_censored'."""
    idx = [i for i, t in enumerate(gt["task"]) if t == "Dodge"]
    if not idx:
        return "abstain", None
    end = idx[0]
    while end + 1 < len(gt["task"]) and gt["task"][end + 1] == "Dodge":
        end += 1
    seg = [t_of(idx[0]), t_of(end + 1)]
    return ("boundary_censored" if idx[0] == 0 else f"{seg[0] / 1000:.3f}"), seg


def wh_moments(gt: dict, events: list[dict] | None, t1_ms: int) -> list[int]:
    """events.json near_miss / dodge times (GT Dodge start and closest approach when unpublished) + the far moment."""
    if events is not None:
        ts = [e["params"].get("t_min_ms", e["t0_ms"]) for e in events if e["type"] in ("near_miss", "dodge")]
    else:
        lab, seg = c4_wh(gt)
        ts = ([seg[0]] if seg else []) + ([t_of(int(np.nanargmin(gt["dist"])))] if np.isfinite(gt["dist"]).any() else [])
    inner = [i for i in range(len(gt["task"])) if 2000 <= t_of(i) <= t1_ms - 2000 and np.isfinite(gt["dist"][i])]
    if inner:
        ts.append(t_of(max(inner, key=lambda i: gt["dist"][i])))
    out = []
    for t in sorted(ts):
        if not out or t - out[-1] >= 500:
            out.append(int(t))
    return out


def wh_actor(t_ms: int, events: list[dict] | None, tracks: list[dict], gt: dict) -> str | None:
    """Person entity of a moment: the person actor of the near_miss / dodge event the moment comes from, else the person
    whose tracks.json root is nearest the GT worker at t_ms (within WORKER_MATCH_M); None when neither holds."""
    for e in events or []:
        ps = [a for a in e["actor_ids"] if a.startswith("P")]
        if e["type"] in ("near_miss", "dodge") and e["params"].get("t_min_ms", e["t0_ms"]) == t_ms and ps:
            return ps[0]
    wx, wy = _at(gt, "wxy", t_ms)
    best = None
    for tr in tracks:
        p = min(tr["pts"], key=lambda q: abs(q[0] - t_ms), default=None)
        if tr["cls"] != "person" or p is None or abs(p[0] - t_ms) > ROOT_DT_MS:
            continue
        d = math.hypot(p[1] - wx, p[2] - wy)
        if d <= WORKER_MATCH_M and (best is None or d < best[0]):
            best = (d, tr["entity_id"])
    return best[1] if best else None


def ghost(track: list[tuple[int, float, float, float]], t_ms: int, actions: list[str], horizon_s: float) -> list | None:
    """Geometric extrapolation [t_ms, x, y] every 0.5 s from the speed and yaw rate measured on the last 0.5 s before t
    (prefix only): stop ramps the speed to 0 at the horizon, slow to half, turn keeps the yaw rate, else straight."""
    past = [p for p in track if t_ms - 500 <= p[0] <= t_ms]
    if len(past) < 2 or past[-1][0] == past[0][0]:
        return None
    (ta, xa, ya, ha), (tb, xb, yb, hb) = past[0], past[-1]
    dt = (tb - ta) / 1000
    v0, w = math.hypot(xb - xa, yb - ya) / dt, math.remainder(hb - ha, 2 * math.pi) / dt if "turn" in actions else 0.0
    end = 0.0 if "stop" in actions else 0.5 if "slow" in actions else 1.0
    pts, x, y, h = [[t_ms, round(xb, 3), round(yb, 3)]], xb, yb, hb
    for k in range(1, int(horizon_s * 2) + 1):
        f = k / (horizon_s * 2)
        v = v0 * (1 + (end - 1) * f)
        h += w * 0.5
        x, y = x + v * 0.5 * math.cos(h), y + v * 0.5 * math.sin(h)
        pts.append([t_ms + k * 500, round(x, 3), round(y, 3)])
    return pts


# --- C3 geometry ------------------------------------------------------------------------------------------------------

def _ray(cam: dict, u: float, v: float) -> tuple[np.ndarray, np.ndarray]:
    M = np.array(cam["cam2world"], float).reshape(4, 4, order="F")
    fx, fy, cx, cy = cam["K"]
    d = M[:3, :3] @ np.array([(u - cx) / fx, -(v - cy) / fy, -1.0])
    return M[:3, 3], d / np.linalg.norm(d)


def floor_range(cam: dict, u: float, v: float) -> tuple[float, np.ndarray] | None:
    """Range (m) along the pixel ray to the floor z=0 and the floor point; None when the ray misses the floor."""
    o, d = _ray(cam, u, v)
    if d[2] >= -1e-6:
        return None
    s = -o[2] / d[2]
    return s, o + s * d


def ground_check(cam: dict, box: list[float], depth_gray: np.ndarray | None, fit: tuple[float, float]) -> dict:
    """ground_poly = box bottom edge back-projected to the floor; depth test on the lower half of the box: residual =
    median(floor range - observed range); support when it exceeds DEPTH_TAU_M (a surface stands in front of the floor).
    fit = (a, b) of range_m = exp((a - gray) / b)."""
    x1, y1, x2, y2 = (box[0] * cam["w"] / 1000, box[1] * cam["h"] / 1000, box[2] * cam["w"] / 1000, box[3] * cam["h"] / 1000)
    poly = [[round(float(p[1][0]), 3), round(float(p[1][1]), 3)] for p in (floor_range(cam, x1, y2), floor_range(cam, x2, y2)) if p]
    res = []
    if depth_gray is not None:
        sy, sx = depth_gray.shape[0] / cam["h"], depth_gray.shape[1] / cam["w"]
        for u in np.linspace(x1, x2, 8):
            for v in np.linspace((y1 + y2) / 2, y2, 6):
                fr = floor_range(cam, u, v)
                g = depth_gray[min(int(v * sy), depth_gray.shape[0] - 1), min(int(u * sx), depth_gray.shape[1] - 1)]
                if fr:
                    res.append(fr[0] - math.exp((fit[0] - float(g)) / fit[1]))
    r = round(float(np.median(res)), 3) if res else None
    return {"ground_poly": poly, "depth_residual_m": r, "geometric_support": bool(r is not None and r > DEPTH_TAU_M)}


def depth_frame(run_dir: Path, cam: str, t_ms: int) -> np.ndarray | None:
    p = run_dir / f"{cam}.depth.mp4"
    if not p.is_file():
        return None
    cap = cv2.VideoCapture(str(p))
    cap.set(cv2.CAP_PROP_POS_FRAMES, ms_frame(t_ms))
    ok, img = cap.read()
    cap.release()
    return img[:, :, 0] if ok else None


# --- PIE ground truth -------------------------------------------------------------------------------------------------

def pie_tracks(vid: str) -> dict[str, dict[int, tuple[list[float], str, str]]]:
    """PIE pedestrian id -> frame -> (box xyxy, action, cross)."""
    setid, v = vid.split("_", 1)
    root = ET.parse(PIE / "annotations" / setid / f"{v}_annt.xml").getroot()
    out: dict[str, dict] = {}
    for tr in root.iter("track"):
        if tr.get("label") != "pedestrian":
            continue
        for b in tr.iter("box"):
            a = {x.get("name"): x.text for x in b.iter("attribute")}
            out.setdefault(a["id"], {})[int(b.get("frame"))] = (
                [float(b.get(k)) for k in ("xtl", "ytl", "xbr", "ybr")], a.get("action"), a.get("cross"))
    return out


def pie_obd(vid: str) -> tuple[np.ndarray, np.ndarray]:
    """Per-frame OBD speed (km/h) and heading (deg)."""
    setid, v = vid.split("_", 1)
    fr = ET.parse(PIE / "annotations_vehicle" / setid / f"{v}_obd.xml").getroot().iter("frame")
    rows = sorted((int(f.get("id")), float(f.get("OBD_speed")), float(f.get("heading_angle"))) for f in fr)
    return np.array([r[1] for r in rows]), np.array([r[2] for r in rows])


def c1_pie(action: str | None, cross: str | None) -> tuple[str, str]:
    """situation_map C1_fleet rules."""
    if cross == "crossing":
        return "crossing", "pie_crossing"
    if cross == "not-crossing" and action == "standing":
        return "waiting", "pie_waiting"
    if action == "walking":
        return "walking_along", "pie_walking"
    return "unrelated", "pie_unrelated"


def pie_moments(vids: list[str], n: int = 12, per_video: int = 3) -> list[tuple[str, str, int]]:
    """(video, ped id, frame): pedestrians at their crossing_point (crossing or not) ranked by the ego slowdown over the
    next 2 s; a quarter of the picks are not-crossing pedestrians as the C1 contrast."""
    cand = []
    for vid in vids:
        setid, v = vid.split("_", 1)
        speed, _ = pie_obd(vid)
        boxed = pie_tracks(vid)
        for p in ET.parse(PIE / "annotations_attributes" / setid / f"{v}_attributes.xml").getroot().iter("pedestrian"):
            f = int(p.get("crossing_point"))
            if 2 * FPS <= f < len(speed) - 2 * FPS and f in boxed.get(p.get("id"), {}):
                cand.append((float(speed[f + 2 * FPS] - speed[f]), p.get("crossing") == "1", vid, p.get("id"), f))
    picks, used = [], {}
    for want_cross, k in ((True, n - n // 4), (False, n // 4)):
        for dv, cross, vid, pid, f in sorted(c for c in cand if c[1] == want_cross):
            if len([p for p in picks if p[3] == want_cross]) >= k:
                break
            if used.get(vid, 0) < per_video:
                picks.append((vid, pid, f, cross))
                used[vid] = used.get(vid, 0) + 1
    return [(v, p, f) for v, p, f, _ in picks]


# --- raw staging (scenes not yet published) ---------------------------------------------------------------------------

def stage_sdg(run_dir: Path) -> str:
    """Minimal scene for vlm.py: raw videos, GT 2D boxes of the worker (P1) and forklift (F1) as tracks 1 and 2."""
    key = sdg.scene_id(run_dir).replace("/", "-").lower()
    d = STAGE_ROOT / key
    cams, ents, obs = [], {"P1": ("person", 1, "character"), "F1": ("forklift", 2, "robot")}, {}
    for cam in sdg.DISPLAY_CAMS:
        cams.append(sdg.cam_entry(sdg.camera_params(run_dir, cam), cam, video=f"video/{cam}.mp4", thumbs=None,
                                  video_w=0, video_h=0))
        (d / "video").mkdir(parents=True, exist_ok=True)
        link = d / "video" / f"{cam}.mp4"
        if not link.exists():
            link.symlink_to(run_dir / f"{cam}.rgb.mp4")
        chunks: dict[int, list] = {}
        for i, line in enumerate(open(run_dir / f"{cam}.object_detection.jsonl")):
            for a in json.loads(line)["agents"].values():
                hit = [e for e, v in ents.items() if v[2] == a["label"]["class"]]
                b = a["annotators"].get("bounding_box_2d_tight_fast")
                if hit and b and i % 3 == 0:
                    chunks.setdefault(t_of(i) // 10000, []).append(
                        [t_of(i), ents[hit[0]][1], ents[hit[0]][0], b["x_min"], b["y_min"], b["x_max"], b["y_max"], 1.0])
                    span = obs.setdefault((hit[0], cam), [t_of(i), t_of(i)])
                    span[1] = t_of(i)
        for c, rows in chunks.items():
            (d / "boxes" / cam).mkdir(parents=True, exist_ok=True)
            (d / "boxes" / cam / f"{c:05d}.json").write_text(json.dumps({"rows": rows}))
    n = sum(1 for _ in open(run_dir / "ceiling_00.object_detection.jsonl"))
    _stage_common(d, key, sdg.scene_id(run_dir), "warehouse", t_of(n), cams,
                  [(e, v[0], [(c, v[1], *obs[(e, c)]) for c in sdg.DISPLAY_CAMS if (e, c) in obs]) for e, v in ents.items()],
                  {"sdg_run": run_dir.name})
    return key


def stage_pie(vid: str, tracks: dict) -> str:
    """Minimal fleet scene: the raw PIE clip and PIE pedestrian boxes as tracks P1..Pn (order of first appearance)."""
    setid, v = vid.split("_", 1)
    key = f"fl-pie-{vid}".lower()
    d = STAGE_ROOT / key
    (d / "video").mkdir(parents=True, exist_ok=True)
    link = d / "video" / "dashcam.mp4"
    if not link.exists():
        link.symlink_to(PIE / "clips" / setid / f"{v}.mp4")
    cal = json.loads((PIE / "calibration_data.json").read_text())
    K = cal["K"]
    cams = [{"id": "dashcam", "w": cal["dim"][0], "h": cal["dim"][1], "K": [K[0][0], K[1][1], K[0][2], K[1][2]],
             "video": "video/dashcam.mp4", "offset_ms": 0}]
    chunks: dict[int, list] = {}
    order = sorted(tracks, key=lambda p: min(tracks[p]))
    for k, pid in enumerate(order, 1):
        for f, (box, _, _) in tracks[pid].items():
            if f % 3 == 0:
                chunks.setdefault(t_of(f) // 10000, []).append([t_of(f), k, "person", *box, 1.0])
    for c, rows in chunks.items():
        (d / "boxes" / "dashcam").mkdir(parents=True, exist_ok=True)
        (d / "boxes" / "dashcam" / f"{c:05d}.json").write_text(json.dumps({"rows": rows}))
    n = len(pie_obd(vid)[0])
    _stage_common(d, key, f"fl/pie/{vid}", "fleet", t_of(n), cams,
                  [(f"P{k}", "person", [("dashcam", k, t_of(min(tracks[p])), t_of(max(tracks[p])))])
                   for k, p in enumerate(order, 1)], {"pie_video": vid})
    return key


def _stage_common(d: Path, key: str, scene_id: str, ws: str, t1: int, cams: list, ents: list, sources: dict) -> None:
    """ents: (entity_id, kind, [(cam_id, local_track_id, t0_ms, t1_ms)])."""
    (d / "entities.json").write_text(json.dumps({"entities": [
        {"entity_id": e, "kind": kind, "observations": [{"cam_id": c, "local_track_id": lt, "t0_ms": a, "t1_ms": b}
                                                       for c, lt, a, b in obs]} for e, kind, obs in ents]}))
    (d / "manifest.json").write_text(json.dumps({"scene_id": scene_id, "scene_key": key, "workspace": ws, "t0_ms": 0,
                                                 "t1_ms": t1, "cams": cams, "sources": sources,
                                                 "assets": {"entities": "entities.json", "boxes_dir": "boxes"}}))


# --- per-scene jobs ---------------------------------------------------------------------------------------------------

def _call(fn, *a):
    try:
        return fn(*a), None
    except vlm.VLMUnavailable as e:
        return None, f"{fn.__name__}: {e}"


def wh_scene(key: str, scene: Path, run_dir: Path, pool: ThreadPoolExecutor) -> tuple[list, list, list]:
    """(records, labels, errors) of one SDG scene; `scene` is the published or staged directory."""
    m = json.loads((scene / "manifest.json").read_text())
    ents = json.loads((scene / m["assets"]["entities"]).read_text())["entities"]
    persons = [e["entity_id"] for e in ents if e["kind"] == "person"]
    fork = next(EntityRef(scene_key=key, entity_id=e["entity_id"], kind="forklift") for e in ents if e["kind"] == "forklift")
    events = json.loads((scene / m["assets"]["events"]).read_text())["events"] if "events" in m["assets"] else None
    gt = sdg_gt(run_dir)
    ts = wh_moments(gt, events, m["t1_ms"])
    if "tracks" in m["assets"]:
        tracks = json.loads((scene / m["assets"]["tracks"]).read_text())["tracks"]
        who = {t: wh_actor(t, events, tracks, gt) for t in ts}
    else:  # staged from raw data: stage_sdg's only person entity is the GT worker
        who = {t: persons[0] for t in ts}
    pref = lambda eid: EntityRef(scene_key=key, entity_id=eid, kind="person")  # noqa: E731
    found = Counter(e for e in who.values() if e)
    person = pref(found.most_common(1)[0][0] if found else persons[0])
    if "forklift" in m["assets"]:
        fl = next(f for f in json.loads((scene / m["assets"]["forklift"]).read_text())["forklifts"] if f["entity_id"] == fork.entity_id)
        track = [(p["t_ms"], p["x"], p["y"], p["heading"]) for p in fl["track"]]
    else:
        track = [(int(t), float(x), float(y), float(h)) for t, (x, y), h in zip(gt["t_ms"], gt["xy"], gt["heading"])]
    t_c3 = ts[0] if ts else m["t1_ms"] // 2
    dfit = m["sources"].get("depth_log_fit", {})  # P-W1's per-run fit when the scene is published
    fit = (dfit.get("a", DEPTH_A), dfit.get("b", DEPTH_B))
    jobs = [(vlm.situation, key, t, [pref(who[t]), fork] if who[t] else [fork]) for t in ts]
    jobs += [(vlm.predict_next, key, t, fork) for t in ts if t >= vlm.PREFIX_MS]  # C2 needs the full 2 s prefix
    jobs += [(vlm.ground_objects, key, "ceiling_00", t_c3, C3_VOCAB), (vlm.localize, key, _cam_of(scene, m, person.entity_id),
             m["t0_ms"], m["t1_ms"], C4_QUERY), (vlm.describe_region, key, _cam_of(scene, m, fork.entity_id), t_c3, fork.entity_id)]
    done = list(pool.map(lambda j: _call(*j), jobs))
    recs, errs = [r for r, _ in done if r], [e for _, e in done if e]
    labels, ref = [], "labels/warehouse_rules.json"
    for r in recs:
        p = r.payload
        if r.task == "C1" and not who.get(r.t_ms):
            r.verification = vlm_ver("unverified", "no person entity resolved to the GT worker", None)
        elif r.task == "C1":
            lab, rule = c1_wh(gt, r.t_ms)
            labels.append(_label(key, r.t_ms, [pref(who[r.t_ms]).model_dump(), fork.model_dump()], "C1", lab, rule))
            r.verification = vlm_ver("verified" if p["label"] == lab else "conflict", f"situation_map rule {rule}", ref)
        elif r.task == "C2":
            if r.t_ms + 2000 <= m["t1_ms"]:
                v0, v2 = _at(gt, "speed", r.t_ms), _at(gt, "speed", r.t_ms + 2000)
                h0, h2 = (math.degrees(_at(gt, "heading", t)) for t in (r.t_ms, r.t_ms + 2000))
                act = c2_truth(v0, v2, h0, h2, WH_RULES)
                labels.append(_label(key, r.t_ms, [fork.model_dump()], "C2", ",".join(act), "c2_rules"))
                p["actual"] = {"actions": act, "source": "sdg gt bbox3d"}
                r.verification = vlm_ver("verified" if set(act) == set(p["actions"]) else "conflict", "c2 rules vs forklift gt", ref)
            p["ghost"] = ghost(track, r.t_ms, p["actions"], p["horizon_s"])
        elif r.task == "C3":
            cam = next(c for c in m["cams"] if c["id"] == r.cam_id)
            dg = depth_frame(run_dir, r.cam_id, r.t_ms)
            for o in p["objects"]:
                o.update(ground_check(cam, o["box_0_1000"], dg, fit))
            r.verification = vlm_ver("verified" if any(o["geometric_support"] for o in p["objects"]) else "unverified",
                                     "depth ray test" if dg is not None else "no depth video", None)
        elif r.task == "C4":
            lab, seg = c4_wh(gt)
            labels.append(_label(key, r.t_ms, [person.model_dump()], "C4", lab, "c4_dodge_start",
                              None if seg is None else f"first Dodge run [{seg[0]}, {seg[1]}) ms"))
            if lab == "abstain":
                ok, how = not p["segments"], "abstain expected (no Dodge)"
            elif lab == "boundary_censored" or not p["segments"]:
                ok, how = None, "boundary_censored" if lab == "boundary_censored" else "model abstained, Dodge present"
            else:
                err = abs(p["segments"][0]["start_s"] - float(lab))
                ok, how = err <= 1.0, f"start error vs first Dodge frame = {err:.2f} s"
            r.verification = vlm_ver({True: "verified", False: "conflict", None: "unverified"}[ok], how, ref)
        else:
            r.verification = vlm_ver("unverified", "no labels", None)
    return recs, labels, errs


def fl_scene(key: str, scene: Path, vid: str, moments: list[tuple[str, int]], tracks: dict,
             pool: ThreadPoolExecutor) -> tuple[list, list, list]:
    """C1 + C2 at each (PIE ped id, frame) moment; the PIE pedestrian is matched to the scene entity by box IoU."""
    m = json.loads((scene / "manifest.json").read_text())
    speed, heading = pie_obd(vid)
    jobs, meta = [], []
    for pid, f in moments:
        ref = _match_entity(key, scene, m, t_of(f), tracks[pid][f][0])
        if ref is None:
            continue
        jobs += [(vlm.situation, key, t_of(f), [ref]), (vlm.predict_next, key, t_of(f), ref)]
        meta += [(pid, f, ref)] * 2
    done = list(pool.map(lambda j: _call(*j), jobs))
    recs, labels, errs, src = [], [], [e for _, e in done if e], "labels/fleet_rules.json"
    for (r, _), (pid, f, ref) in zip(done, meta):
        if r is None:
            continue
        recs.append(r)
        if r.task == "C1":
            _, action, cross = tracks[pid][f]
            lab, rule = c1_pie(action, cross)
            labels.append(_label(key, r.t_ms, [ref.model_dump()], "C1", lab, rule, f"PIE ped {pid}"))
            r.verification = vlm_ver("verified" if r.payload["label"] == lab else "conflict", f"situation_map rule {rule}", src)
        else:
            act = c2_truth(speed[f], speed[f + 2 * FPS], heading[f], heading[f + 2 * FPS], FL_RULES)
            labels.append(_label(key, r.t_ms, [ref.model_dump()], "C2", ",".join(act), "c2_rules", "ego OBD"))
            r.payload["actual"] = {"actions": act, "source": "pie obd"}
            r.verification = vlm_ver("verified" if set(act) == set(r.payload["actions"]) else "conflict",
                                     "c2 rules vs PIE OBD", src)
    return recs, labels, errs


def _match_entity(key: str, scene: Path, m: dict, t_ms: int, box: list[float]) -> EntityRef | None:
    """Scene entity whose dashcam box near t overlaps the PIE box best (IoU >= 0.3)."""
    p = scene / m["assets"].get("boxes_dir", "boxes") / "dashcam" / f"{t_ms // 10000:05d}.json"
    rows = [r for r in json.loads(p.read_text())["rows"] if abs(r[0] - t_ms) <= 100] if p.is_file() else []
    best = max(rows, key=lambda r: _iou(r[3:7], box), default=None)
    if best is None or _iou(best[3:7], box) < 0.3:
        return None
    for e in json.loads((scene / m["assets"]["entities"]).read_text())["entities"]:
        for o in e["observations"]:
            if o["cam_id"] == "dashcam" and o["local_track_id"] == best[1] and o["t0_ms"] <= t_ms <= o["t1_ms"]:
                return EntityRef(scene_key=key, entity_id=e["entity_id"], kind=e["kind"])
    return None


def _iou(a, b) -> float:
    w, h = min(a[2], b[2]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[1], b[1])
    inter = max(w, 0) * max(h, 0)
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter + 1e-9)


def _cam_of(scene: Path, m: dict, eid: str) -> str:
    """Camera observing the entity longest."""
    seen = {c["id"]: 0 for c in m["cams"]}
    for e in json.loads((scene / m["assets"]["entities"]).read_text())["entities"]:
        for o in e["observations"] if e["entity_id"] == eid else []:
            seen[o["cam_id"]] = seen.get(o["cam_id"], 0) + o["t1_ms"] - o["t0_ms"]
    return max(seen, key=lambda c: seen[c])


def vlm_ver(status: str, method: str, ref: str | None) -> CosmosVerification:
    return CosmosVerification(status=status, method=method, ref=ref)


def _label(key, t_ms, refs, task, label, rule, note=None) -> dict:
    return {"scene_key": key, "t_ms": t_ms, "actor_refs": refs, "task": task, "label": label, "source": "rule",
            "rule_id": rule, "annotator": None, "note": note}


def merge_labels(old: list[dict], new: list[dict]) -> list[dict]:
    """Labels keyed by (scene_key, task, t_ms, actor ids): new ones replace an equal key, every other label stays."""
    key = lambda x: (x["scene_key"], x["task"], x["t_ms"], tuple(sorted(r["entity_id"] for r in x["actor_refs"])))  # noqa: E731
    out = {key(x): x for x in old}
    out.update((key(x), x) for x in new)
    return sorted(out.values(), key=lambda x: (x["scene_key"], x["t_ms"], x["task"]))


def rev_tag(revs: dict[str, str]) -> str:
    """'C1:px-2 C2:px-1 ...': the prompt revision of each task, for document-level rev fields."""
    return " ".join(f"{t}:{r}" for t, r in sorted(revs.items()))


def stage_records(key: str, recs: list, inputs: dict[str, str]) -> Path:
    d = publish.tmp_dir(CARD, key, out_root=OUT)
    (d / "cosmos").mkdir(exist_ok=True)
    rev = rev_tag({r.task: r.prompt_rev for r in recs})
    doc = {"schema_version": "1", "analysis_rev": rev, "producer_commit": commit(), "input_artifact_hashes": inputs,
           "scene_key": key, "records": [r.model_dump() for r in sorted(recs, key=lambda r: (r.t_ms, r.task))]}
    (d / "cosmos" / f"{key}.json").write_text(json.dumps(doc, ensure_ascii=False))
    (d / publish.PATCH).write_text(json.dumps({"assets": {"cosmos": f"cosmos/{key}.json"},
                                               "models": {"vlm": f"{os.environ.get('VLM_MODEL', '')}@{rev}"}}))
    return d / "cosmos" / f"{key}.json"


def load_records(task: str) -> list[dict]:
    """Records of one task over all scenes, each with scene_key; a published cosmos file wins over this card's staged one."""
    docs = {}
    for p in [*(OUT / "_tmp" / CARD).glob("*/cosmos/*.json"), *OUT.glob("*/cosmos/*.json")]:
        d = json.loads(p.read_text())
        docs[d["scene_key"]] = d
    return [{**r, "scene_key": k} for k, d in sorted(docs.items()) for r in d["records"] if r["task"] == task]


def load_labels(task: str) -> dict[tuple[str, int], list[dict]]:
    """(scene_key, t_ms) -> labels of one task from fixtures/labels/*.json (rule and human)."""
    out: dict = {}
    for p in sorted(LABELS.glob("*.json")):
        for lab in json.loads(p.read_text())["labels"]:
            if lab["task"] == task:
                out.setdefault((lab["scene_key"], lab["t_ms"]), []).append(lab)
    return out


def write_eval(name: str, result: dict) -> None:
    d = OUT / "_eval"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps(result, indent=1))


def use_scene(scene: Path, is_pub: bool) -> None:
    """Point vlm.py at the scene; staged scenes get their own record cache so published runs never reuse their frames."""
    os.environ["ASSET_DIR"] = str(scene.parent)
    os.environ["RUNS_DIR"] = str(STAGE_ROOT.parent / "vlm_runs" / ("published" if is_pub else "raw"))


def published(ws: str) -> dict[str, Path]:
    """Published scenes of the workspace that have video and entities (GPS-only trips do not count)."""
    out = {}
    for p in OUT.glob("*/manifest.json"):
        m = json.loads(p.read_text())
        if m.get("workspace") == ws and m["cams"] and "entities" in m["assets"]:
            out[p.parent.name] = p.parent
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", choices=["warehouse", "fleet"], required=True)
    ap.add_argument("--raw", action="store_true", help="also run scenes that are not published yet (staged from raw data)")
    ap.add_argument("--limit", type=int, default=0, help="at most N scenes (0 = all)")
    a = ap.parse_args()
    os.environ.setdefault("VLM_URL", "http://localhost:8101/v1")
    os.environ.setdefault("VLM_MODEL", "nvidia/cosmos3-nano-reasoner")
    LOGS.mkdir(parents=True, exist_ok=True)
    chat = vlm._chat

    def slotted(*args, **kw):  # one lease slot and one elapsed sample per HTTP request
        with lease.vlm_slot(owner=CARD):
            t0 = time.monotonic()
            try:
                return chat(*args, **kw)
            finally:
                ELAPSED.append(round((time.monotonic() - t0) * 1000))
    vlm._chat = slotted
    pub = published(a.workspace)
    all_labels, report = [], {"published": [], "pending": [], "errors": []}
    with lease.acquire(cpu=8, owner=CARD), ThreadPoolExecutor(lease.VLM_CONCURRENCY) as pool:
        if a.workspace == "warehouse":
            runs = {sdg.scene_id(r).replace("/", "-").lower(): r for r in sorted(sdg.SDG_ROOT.iterdir()) if sdg.RUN_RE.match(r.name)}
            keys = [k for k in runs if k in pub or a.raw][: a.limit or None]
            for key in keys:
                scene = pub.get(key) or STAGE_ROOT / stage_sdg(runs[key])
                use_scene(scene, key in pub)
                recs, labels, errs = wh_scene(key, scene, runs[key], pool)
                inputs = {"raw:ceiling_00.object_detection.jsonl": sha(runs[key] / "ceiling_00.object_detection.jsonl")}
                _finish(key, recs, labels, errs, inputs, key in pub, all_labels, report)
        else:
            vids = sorted(f"{p.parent.name}_{p.stem}" for p in (PIE / "clips").glob("set*/video_*.mp4"))
            pubvid = {k: v for k, v in ((k, re.search(r"(set\d\d)\D*(video_\d{4})", k + json.dumps(json.loads(
                (s / "manifest.json").read_text())["sources"]))) for k, s in pub.items()) if v}
            vids = [v for v in vids if a.raw or v in {f"{x[1]}_{x[2]}" for x in pubvid.values()}]
            picks = pie_moments(vids)
            for vid in sorted({p[0] for p in picks})[: a.limit or None]:
                key = next((k for k, x in pubvid.items() if f"{x[1]}_{x[2]}" == vid), None)
                tracks = pie_tracks(vid)
                scene = pub[key] if key else STAGE_ROOT / stage_pie(vid, tracks)
                key = key or scene.name
                use_scene(scene, key in pub)
                recs, labels, errs = fl_scene(key, scene, vid, [(p, f) for v, p, f in picks if v == vid], tracks, pool)
                setid, v = vid.split("_", 1)
                inputs = {f"raw:pie/{setid}/{v}_annt.xml": sha(PIE / "annotations" / setid / f"{v}_annt.xml")}
                _finish(key, recs, labels, errs, inputs, key in pub, all_labels, report)
    lab_path = LABELS / f"{a.workspace}_rules.json"
    LABELS.mkdir(parents=True, exist_ok=True)
    old = json.loads(lab_path.read_text())["labels"] if lab_path.is_file() else []
    lab_path.write_text(json.dumps({"schema_version": "1", "analysis_rev": rev_tag(vlm.PROMPT_REV), "producer_commit": commit(),
                                    "input_artifact_hashes": {}, "labels": merge_labels(old, all_labels)},
                                   ensure_ascii=False, indent=0))
    el = sorted(ELAPSED)
    report["elapsed_ms"] = {"n": len(el), "median": el[len(el) // 2] if el else None, "p90": el[int(len(el) * 0.9)] if el else None}
    (LOGS / f"elapsed_{a.workspace}.json").write_text(json.dumps({"elapsed_ms": ELAPSED}))
    (LOGS / f"batch_{a.workspace}.json").write_text(json.dumps(report, indent=1))
    print(json.dumps({k: (len(v) if isinstance(v, list) else v) for k, v in report.items()}))


def _finish(key, recs, labels, errs, inputs, is_pub, all_labels, report) -> None:
    all_labels += labels
    report["errors"] += [f"{key}: {e}" for e in errs]
    if not recs:
        return
    path = stage_records(key, recs, inputs)
    print(f"{key}: {len(recs)} records -> {path}", file=sys.stderr)
    if is_pub:
        try:
            publish.publish(CARD, key, out_root=OUT)
            report["published"].append(key)
        except publish.PublishError as e:
            report["errors"].append(str(e))
            report["pending"].append(key)
    else:
        report["pending"].append(key)


if __name__ == "__main__":
    main()
