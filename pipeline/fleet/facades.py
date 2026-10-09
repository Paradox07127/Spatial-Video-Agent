"""F8: nearest building facade along each image column (D2 `_facades`, sim/demos/claim-pack-v3/geo.py:126-164).

The ray of each column at the horizon row is cast on the ground plane against the OSM building outlines; a building's
image span is the columns where it is the nearest hit.
"""
from __future__ import annotations

import math

import cv2
import numpy as np

from pipeline.fleet import pie

FACADE_M = 250.0  # farthest facade reported
COL_STEP = 3  # px of the source frame between cast columns


def _camera(heading_deg: float, cal: dict):
    """(K, D, rotation world->camera) with world = (east, north, up) metres from the camera."""
    K, D = np.array(cal["K"], float), np.array(cal["D"], float).reshape(4, 1)
    h, p = math.radians(heading_deg), math.radians(-cal["cam_pitch_deg"])
    f, r, u = np.array([math.sin(h), math.cos(h), 0]), np.array([math.cos(h), -math.sin(h), 0]), np.array([0, 0, 1.0])
    return K, D, np.stack([r, math.sin(p) * f - math.cos(p) * u, math.cos(p) * f + math.sin(p) * u])


def facades(o: dict, lat: float, lon: float, heading_deg: float, cal: dict) -> dict[str, tuple[float, float, float]]:
    """bid -> (u0, u1, median distance m) of each building that is the nearest facade in some column; u in source px."""
    p0 = np.array(pie.xy(lat, lon), float)
    near = np.nonzero(np.hypot(*(o["bld_c"] - p0).T) - o["bld_r"] <= FACADE_M)[0]
    if not len(near):
        return {}
    K, D, R = _camera(heading_deg, cal)
    e1 = np.concatenate([o["bld_rings"][i][:-1] for i in near]) - p0
    e2 = np.concatenate([o["bld_rings"][i][1:] for i in near]) - p0
    owner = np.concatenate([np.full(len(o["bld_rings"][i]) - 1, i) for i in near])
    # A fix inside an outline (GPS error next to a facade) would see that building's walls from inside in every column:
    # drop outlines that contain the camera (odd number of edges crossing the ray along +x).
    with np.errstate(divide="ignore", invalid="ignore"):
        cross = ((e1[:, 1] > 0) != (e2[:, 1] > 0)) & (e1[:, 0] - e1[:, 1] * (e2[:, 0] - e1[:, 0]) / (e2[:, 1] - e1[:, 1]) > 0)
    inside = np.bincount(owner, weights=cross, minlength=len(o["bld_rings"])) % 2 == 1
    keep = ~inside[owner]
    if not keep.any():
        return {}
    e1, e2, owner = e1[keep], e2[keep], owner[keep]
    h = math.radians(heading_deg)
    fwd = (np.array([[math.sin(h), math.cos(h), 0.0]]) @ R.T).reshape(-1, 1, 3)
    vh = cv2.fisheye.projectPoints(fwd, np.zeros(3), np.zeros(3), K, D)[0][0, 0, 1]  # row of the level forward direction
    us = np.arange(0, cal["dim"][0], COL_STEP)
    ray = cv2.fisheye.undistortPoints(np.stack([us, np.full(len(us), vh)], 1).reshape(-1, 1, 2).astype(float), K, D)
    w = np.concatenate([ray.reshape(-1, 2), np.ones((len(us), 1))], 1) @ R
    d = w[:, :2] / np.hypot(w[:, 0], w[:, 1])[:, None]
    ex, ey = (e2 - e1).T
    den = d[:, :1] * ey - d[:, 1:] * ex
    with np.errstate(divide="ignore", invalid="ignore"):
        t = (e1[:, 0] * ey - e1[:, 1] * ex) / den
        s = (e1[:, 0] * d[:, 1:] - e1[:, 1] * d[:, :1]) / den
    # Hits nearer than 1 m are the footprint the camera stands in or touches.
    t = np.where((s >= 0) & (s <= 1) & (t > 1) & (t <= FACADE_M), t, np.inf)
    k = t.argmin(1)
    hits: dict[str, list[tuple[int, float]]] = {}
    for c, u in enumerate(us):
        if np.isfinite(t[c, k[c]]):
            hits.setdefault(o["buildings"][owner[k[c]]]["id"], []).append((int(u), float(t[c, k[c]])))
    w_px = cal["dim"][0]
    return {bid: (float(min(u for u, _ in x)), float(min(max(u for u, _ in x) + COL_STEP, w_px)),
                  float(np.median([dd for _, dd in x]))) for bid, x in hits.items()}
