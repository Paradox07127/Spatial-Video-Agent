"""SmartSpaces camera offset scan and sync-gated association.  .venv/bin/python -m pytest pipeline/warehouse/test_sync.py -q"""
import numpy as np

from pipeline.warehouse import smartspaces, triangulate

STEP = 200  # 5 fps pose frames


def _cam(cid, x):
    """Looks straight down from 6 m: OpenGL camera axes = world axes."""
    return {"id": cid, "K": [500.0, 500.0, 320.0, 240.0], "cam2world": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, x, 0, 6, 1]}


CAMS = {"camera": _cam("camera", 0.0), "camera_01": _cam("camera_01", 4.0)}


def _poses(true_off_ms: int, moving: bool) -> dict:
    """Two people walking circles of 0.8 m in the overlap; camera_01's media clock runs true_off_ms behind the scene."""
    rng = np.random.default_rng(0)
    out = {}
    for cid, off in (("camera", 0), ("camera_01", true_off_ms)):
        g = triangulate.Geo(CAMS[cid])
        frames = []
        for t in range(0, 20000, STEP):
            s = (t + off) / 1000 * (1.5 if moving else 0.0)
            dets = []
            for tid, (cx, cy, ph) in enumerate(((2.0, -1.5, 0.0), (2.0, 1.5, 2.0)), 1):
                p = np.array([cx + 0.8 * np.cos(s + ph), cy + 0.8 * np.sin(s + ph), 0.0]) + rng.normal(0, 0.03, 3) * [1, 1, 0]
                (u, v), _ = g.project(p)
                dets.append([tid, "person", u - 10, v - 60, u + 10, v, 0.9, [[u, v, 0.0]] * 17])
            frames.append([t, dets])
        out[cid] = {"fps": 1000 / STEP, "frames": frames}
    return out


def _scan(poses):
    geos = {c: triangulate.Geo(CAMS[c]) for c in CAMS}
    return smartspaces.offset_scan(geos, triangulate.person_tracks(poses), "camera", "camera_01", STEP)


def test_offset_scan_finds_the_true_offset():
    scan = _scan(_poses(400, moving=True))
    assert sorted(scan) == list(range(-1000, 1001, STEP))
    assert smartspaces.clear_minimum(scan) == 400


def test_people_standing_still_give_no_clear_minimum():
    assert smartspaces.clear_minimum(_scan(_poses(400, moving=False))) is None


def test_unsynced_cameras_are_never_associated():
    geos, tracks = {c: triangulate.Geo(CAMS[c]) for c in CAMS}, triangulate.person_tracks(_poses(0, moving=True))
    assert sorted(len(m) for m, _ in triangulate.associate(geos, tracks, linked={"camera", "camera_01"})) == [2, 2]
    assert sorted(len(m) for m, _ in triangulate.associate(geos, tracks, linked={"camera"})) == [1, 1, 1, 1]
