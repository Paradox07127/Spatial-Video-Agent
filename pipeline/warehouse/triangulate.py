"""P5: cross-camera person association (ground distance), multi-view skeletons and roots for one SDG run.

  python pipeline/warehouse/triangulate.py RUN

Reads pose2d/<cam>.json (pose2d.py) and the run's camera_params. Writes <scene>/skeleton.json and
_work/<scene_key>/persons.json = {entities: [...], tracks: [...], boxes: {cam: [[t_ms, tid, cls, x1, y1, x2, y2, conf]]}}
for run_all.py. Joints come from >= 2 views (DLT + RANSAC, reprojection < 8 px, bone-length check, Savitzky-Golay
smoothing); frames with one view get a root only (ankle or box bottom) and xyz null.
"""
from __future__ import annotations

import argparse
import itertools
import sys
from collections import Counter
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common import lease  # noqa: E402
from pipeline.warehouse import pose2d, sdg  # noqa: E402

KP_CONF = 0.5  # RTMPose SimCC score below which a keypoint is not used
REPROJ_PX = 8.0
ANKLE_H = 0.08  # m, ankle joint height above the floor (anthropometric)
ANKLES = (15, 16)
ASSOC_M = 1.0  # max median ground distance between two camera tracks of one person
MIN_TRACK = 8  # frames; shorter person tracks are treated as detector noise and dropped
BONE_TOL = 0.25  # relative deviation from the person's median bone length that rejects a joint
BONES = [(5, 7), (7, 9), (6, 8), (8, 10), (11, 13), (13, 15), (12, 14), (14, 16), (5, 6), (11, 12), (5, 11), (6, 12)]
SMOOTH_WIN = 5  # Savitzky-Golay window (frames), polyorder 2
# per-axis 1-sigma root error on frames where the method is the production choice (pipeline/eval/wh_position_error.py)
ROOT_SIGMA = {"tri": 0.09, "ankle": 0.40}


class Geo:
    """Pinhole camera from a manifest cams[] entry (cam2world OpenGL, K source pixels)."""

    def __init__(self, cam: dict):
        M = np.array(cam["cam2world"], float).reshape(4, 4, order="F")
        fx, fy, cx, cy = cam["K"]
        self.K = np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1.0]])
        self.R = M[:3, :3] @ np.diag([1.0, -1, -1])  # OpenCV camera axes -> world
        self.C = M[:3, 3]
        self.P = self.K @ np.c_[self.R.T, -self.R.T @ self.C]

    def ground(self, u: float, v: float, z: float) -> np.ndarray | None:
        d = self.R @ np.linalg.solve(self.K, [u, v, 1.0])
        s = (z - self.C[2]) / d[2] if d[2] else -1
        return (self.C + s * d)[:2] if s > 0 else None

    def project(self, X: np.ndarray) -> tuple[np.ndarray, float]:
        x = self.P @ np.append(X, 1.0)
        return x[:2] / x[2], x[2]


def root_view(g: Geo, det: list) -> tuple[np.ndarray | None, str]:
    """Ankle pixels on the ankle-height plane when an ankle is confident, else the box bottom centre on the floor."""
    kp = det[7]
    a = [kp[j][:2] for j in ANKLES if kp and kp[j][2] >= KP_CONF]
    if a:
        u, v = np.mean(a, axis=0)
        return g.ground(u, v, ANKLE_H), "ankle"
    return g.ground((det[2] + det[4]) / 2, det[5], 0.0), "boxbottom"


def dlt(Ps: list[np.ndarray], uvs: list) -> np.ndarray:
    A = np.concatenate([[u * P[2] - P[0], v * P[2] - P[1]] for P, (u, v) in zip(Ps, uvs)])
    X = np.linalg.svd(A)[2][-1]
    return X[:3] / X[3]


def tri_joint(obs: list[tuple[Geo, list]]) -> tuple[np.ndarray | None, list[int]]:
    """RANSAC over view pairs: the pair whose point has the most views within REPROJ_PX (in front of the camera), refit on them."""
    best, best_in, best_err = None, [], np.inf
    for i, j in itertools.combinations(range(len(obs)), 2):
        X = dlt([obs[i][0].P, obs[j][0].P], [obs[i][1][:2], obs[j][1][:2]])
        inl, err = [], 0.0
        for k, (g, kp) in enumerate(obs):
            x, depth = g.project(X)
            e = float(np.hypot(*(x - kp[:2])))
            if depth > 0 and e < REPROJ_PX:
                inl.append(k)
                err += e
        if len(inl) > len(best_in) or (len(inl) == len(best_in) and inl and err < best_err):
            best, best_in, best_err = X, inl, err
    if len(best_in) < 2:
        return None, []
    if len(best_in) > 2:
        best = dlt([obs[k][0].P for k in best_in], [obs[k][1][:2] for k in best_in])
    return best, best_in


def person_tracks(poses: dict[str, dict]) -> dict[tuple[str, int], dict[int, list]]:
    tr: dict[tuple[str, int], dict[int, list]] = {}
    for cam, pz in poses.items():
        for t, dets in pz["frames"]:
            for d in dets:
                if d[1] == "person":
                    tr.setdefault((cam, d[0]), {})[t] = d
    return {k: v for k, v in tr.items() if len(v) >= MIN_TRACK}


def associate(geos: dict[str, Geo], tracks: dict, linked: set[str] | None = None) -> list[tuple[list, list[float]]]:
    """Greedy single-linkage on the median ground distance of camera-track pairs (>= 3 common frames, < ASSOC_M);
    two tracks of one camera never share a cluster when their time spans overlap. Only cameras in `linked` (None = all)
    are paired, since equal timestamps mean equal times only on synchronised cameras. Returns (members, link distances)."""
    ground = {k: {t: root_view(geos[k[0]], d)[0] for t, d in v.items()} for k, v in tracks.items()}
    span = {k: (min(v), max(v)) for k, v in tracks.items()}
    pairs = []
    for a, b in itertools.combinations(tracks, 2):
        if a[0] == b[0] or (linked is not None and not {a[0], b[0]} <= linked):
            continue
        common = [t for t in ground[a] if t in ground[b] and ground[a][t] is not None and ground[b][t] is not None]
        if len(common) >= 3:
            d = float(np.median([np.linalg.norm(ground[a][t] - ground[b][t]) for t in common]))
            if d < ASSOC_M:
                pairs.append((d, a, b))
    cl = {k: ([k], []) for k in tracks}
    for d, a, b in sorted(pairs):
        ca, cb = cl[a], cl[b]
        if ca is cb or any(x[0] == y[0] and span[x][0] <= span[y][1] and span[y][0] <= span[x][1] for x in ca[0] for y in cb[0]):
            continue
        merged = (ca[0] + cb[0], ca[1] + cb[1] + [d])
        for k in merged[0]:
            cl[k] = merged
    out = list({id(c): c for c in cl.values()}.values())
    return sorted(out, key=lambda c: min(span[k][0] for k in c[0]))


def _smooth(ts: list[int], xyz: list, step: float) -> None:
    from scipy.signal import savgol_filter
    for j in range(17):
        run: list[int] = []
        for i in range(len(ts) + 1):
            ok = i < len(ts) and xyz[i] is not None and xyz[i][j] is not None
            if ok and run and round((ts[i] - ts[run[-1]]) / step) == 1:
                run.append(i)
                continue
            if len(run) >= SMOOTH_WIN:
                arr = savgol_filter(np.array([xyz[k][j] for k in run]), SMOOTH_WIN, 2, axis=0)
                for k, p in zip(run, arr):
                    xyz[k][j] = p
            run = [i] if ok else []


def skeleton_of(members: list, tracks: dict, geos: dict[str, Geo], step: float) -> dict:
    by_t: dict[int, list] = {}
    for k in members:
        for t, d in tracks[k].items():
            by_t.setdefault(t, []).append((geos[k[0]], d))
    ts = sorted(by_t)
    xyz, conf, nv = [], [], []
    for t in ts:
        views = by_t[t]
        fx, fc, fn = [None] * 17, [0.0] * 17, [0] * 17
        for j in range(17):
            obs = [(g, d[7][j]) for g, d in views if d[7][j][2] >= KP_CONF]
            fc[j] = round(float(np.mean([kp[2] for _, kp in obs])), 3) if obs else 0.0
            if len(obs) >= 2:
                X, inl = tri_joint(obs)
                fx[j], fn[j] = X, len(inl)
            else:
                fn[j] = len(obs)
        xyz.append(fx if any(x is not None for x in fx) else None)
        conf.append(fc)
        nv.append(fn)
    lens = {b: [np.linalg.norm(f[b[0]] - f[b[1]]) for f in xyz if f and f[b[0]] is not None and f[b[1]] is not None] for b in BONES}
    med = {b: float(np.median(v)) for b, v in lens.items() if v}
    for i, f in enumerate(xyz):
        if f is None:
            continue
        for b in BONES:
            if b in med and f[b[0]] is not None and f[b[1]] is not None and abs(np.linalg.norm(f[b[0]] - f[b[1]]) / med[b] - 1) > BONE_TOL:
                j = min(b, key=lambda q: (nv[i][q], conf[i][q]))
                f[j], nv[i][j] = None, 0
        if all(x is None for x in f):
            xyz[i] = None
    _smooth(ts, xyz, step)
    p = {"t_ms": [], "root_world": [], "root_method": [], "root_sigma_m": [], "xyz": [], "conf": [], "n_views": []}
    for i, t in enumerate(ts):
        f = xyz[i]
        ank = [f[j][:2] for j in ANKLES if f is not None and f[j] is not None]
        if ank:
            xy, method = np.mean(ank, axis=0), "tri"
        else:
            use = [r for r in (root_view(g, d) for g, d in by_t[t]) if r[0] is not None and r[1] == "ankle"]
            if not use:  # no confident ankle on the plane: no root for this frame
                continue
            xy, method = np.mean([r[0] for r in use], axis=0), use[0][1]
        p["t_ms"].append(t)
        p["root_world"].append([round(float(v), 3) for v in xy])
        p["root_method"].append(method)
        p["root_sigma_m"].append(ROOT_SIGMA[method])
        p["xyz"].append(None if f is None else [None if x is None else [round(float(v), 3) for v in x] for x in f])
        p["conf"].append(conf[i])
        p["n_views"].append(nv[i])
    return p


def build(poses: dict[str, dict], cams: dict[str, dict], t1_ms: int, linked: set[str] | None = None) -> tuple[dict, dict]:
    """(persons skeleton dict keyed by entity id, persons.json body) for one scene; linked as in associate()."""
    geos = {c: Geo(cams[c]) for c in poses}
    step = 1000 / next(iter(poses.values()))["fps"]
    tracks = person_tracks(poses)
    persons, ents, trs, boxes = {}, [], [], {c: [] for c in poses}
    for n, (members, links) in enumerate(associate(geos, tracks, linked), 1):
        eid = f"P{n}"
        sk = skeleton_of(members, tracks, geos, step)
        if not sk["t_ms"]:
            continue
        persons[eid] = sk
        obs = []
        for cam, tid in sorted(members):
            tt = sorted(tracks[(cam, tid)])
            obs.append({"cam_id": cam, "local_track_id": tid, "t0_ms": tt[0], "t1_ms": min(round(tt[-1] + step), t1_ms),
                        "method": "bytetrack", "conf": round(float(np.mean([d[6] for d in tracks[(cam, tid)].values()])), 3)})
            boxes[cam] += [[t, tid, "person", *d[2:7]] for t, d in tracks[(cam, tid)].items()]
        ents.append({"entity_id": eid, "kind": "person", "observations": obs,
                     "association": {"method": "ground_dist", "conf": round(1 - float(np.mean(links)) / ASSOC_M, 3) if links else 1.0,
                                     "valid": [sk["t_ms"][0], min(round(sk["t_ms"][-1] + step), t1_ms)]}})
        trs.append({"entity_id": eid, "cls": "person", "t0_ms": sk["t_ms"][0], "t1_ms": min(round(sk["t_ms"][-1] + step), t1_ms),
                    "cam_ids": sorted({c for c, _ in members}),
                    "pts": [[t, *xy, s, "observed"] for t, xy, s in zip(sk["t_ms"], sk["root_world"], sk["root_sigma_m"])],
                    "source": Counter(sk["root_method"]).most_common(1)[0][0]})
    return persons, {"entities": ents, "tracks": trs, "boxes": boxes}


def sdg_cams(rd: Path, cams: list[str]) -> dict[str, dict]:
    return {c: sdg.cam_entry(sdg.camera_params(rd, c), c, video=None, thumbs=None, video_w=1920, video_h=1080) for c in cams}


def write(key: str, poses: dict[str, dict], cams: dict[str, dict], t1_ms: int, inputs: list[Path],
          linked: set[str] | None = None) -> dict:
    """skeleton.json into the scene dir and persons.json into the work dir; returns the persons."""
    persons, body = build(poses, cams, t1_ms, linked)
    pose2d.write_json(pose2d.OUT_ROOT / "_tmp" / pose2d.OWNER / key / "skeleton.json",
                      {**pose2d.header(inputs), "fps": round(next(iter(poses.values()))["fps"], 3), "joints": "coco17", "persons": persons})
    pose2d.write_json(pose2d.WORK / key / "persons.json", body)
    return persons


def run(rd: Path) -> dict:
    key = pose2d.scene_key(rd)
    inputs = [pose2d.WORK / key / "pose2d" / f"{c}.json" for c in sdg.DISPLAY_CAMS] + \
             [rd / f"{c}.camera_params.jsonl" for c in sdg.DISPLAY_CAMS]
    return write(key, pose2d.load(key, sdg.DISPLAY_CAMS), sdg_cams(rd, sdg.DISPLAY_CAMS), 10000, inputs)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    a = ap.parse_args()
    rd = pose2d.run_dir(a.run)
    with lease.acquire(cpu=1, owner=pose2d.OWNER):
        persons = run(rd)
    print(f"triangulate {pose2d.scene_key(rd)}: {len(persons)} persons", file=sys.stderr)


if __name__ == "__main__":
    main()
