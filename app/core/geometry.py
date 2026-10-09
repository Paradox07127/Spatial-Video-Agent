"""OpenGL camera rays, metric distances and floor visibility."""
from __future__ import annotations

import base64
import math

import cv2
import numpy as np
from shapely.geometry import Point, Polygon


def intrinsic(K):
    a = np.asarray(K, dtype=float)
    if a.shape == (3, 3):
        return a
    fx, fy, cx, cy = a
    return np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1.]])


def undistort_fisheye(pts, K, D):
    K = intrinsic(K)
    pts = np.asarray(pts, float).reshape(-1, 1, 2)
    return cv2.fisheye.undistortPoints(pts, K, np.asarray(D, float), P=K).reshape(-1, 2)


def ground_point(cam, u, v):
    if cam.get('model') == 'fisheye':
        u, v = undistort_fisheye([[u, v]], cam['K'], cam['dist'])[0]
    fx, fy, cx, cy = cam['K']
    matrix = np.asarray(cam['cam2world'], float).reshape(4, 4, order='F')
    origin = matrix[:3, 3]
    ray = matrix[:3, :3] @ np.array([(u-cx)/fx, -(v-cy)/fy, -1.])
    scale = -origin[2]/ray[2] if abs(ray[2]) > 1e-12 else -1
    return origin[:2] + scale*ray[:2] if scale > 0 else np.full(2, np.nan)


def distance(a, b):
    delta = np.array([a['x']-b['x'], a['y']-b['y']], float)
    return float(np.linalg.norm(delta)), math.hypot(a['sigma_m'], b['sigma_m'])


def floor_visibility(data, t_ms, point):
    ix, iy = (math.floor((point[i]-data['origin'][i])/data['cell_m']) for i in range(2))
    order = data['cam_bit_order']
    sec = t_ms//1000-data['t0_s']
    if not (0 <= ix < data['nx'] and 0 <= iy < data['ny'] and 0 <= sec < len(data['per_sec'])):
        return {c: 'unknown' for c in order}
    raw = base64.b64decode(data['per_sec'][sec])
    offset = (iy*data['nx']+ix)*len(order)
    return {c: data['states'][str(raw[offset+i])] for i, c in enumerate(order)}


def coverage(cams, zone, occluders):
    polygon = zone if isinstance(zone, Polygon) else Polygon(zone)
    lo_x, lo_y, hi_x, hi_y = polygon.bounds
    # 0.2 m cells and standing-body heights follow the D3 coverage calculation.
    step = .2
    pts = np.array([(x, y) for x in np.arange(lo_x+step/2, hi_x, step)
                    for y in np.arange(lo_y+step/2, hi_y, step) if polygon.covers(Point(x, y))]).reshape(-1, 2)
    per_cam, visible, geometric = {}, [], []
    for cam in cams:
        matrix = np.asarray(cam['cam2world'], float).reshape(4, 4, order='F')
        xyz = np.c_[pts, np.zeros(len(pts)), np.ones(len(pts))] @ np.linalg.inv(matrix).T
        fx, fy, cx, cy = cam['K']
        if cam.get('model')=='fisheye' and len(pts):
            cv_xyz=np.c_[xyz[:,0],-xyz[:,1],-xyz[:,2]].reshape(-1,1,3)
            uv,_=cv2.fisheye.projectPoints(cv_xyz,np.zeros(3),np.zeros(3),intrinsic(cam['K']),np.asarray(cam['dist'],float))
            u,v=uv.reshape(-1,2).T
        else:
            with np.errstate(divide='ignore', invalid='ignore'):
                u, v = cx+fx*xyz[:, 0]/-xyz[:, 2], cy-fy*xyz[:, 1]/-xyz[:, 2]
        geo = (xyz[:, 2]<0)&(u>=0)&(u<cam['w'])&(v>=0)&(v<cam['h'])
        seen = geo.copy()
        center = matrix[:3, 3]
        for obstacle in occluders:
            hidden = np.ones(len(pts), bool)
            for height in (1., 1.5):
                end = np.c_[pts, np.full(len(pts), height)]
                rays = center+np.linspace(.02,.98,40)[None,:,None]*(end[:,None,:]-center)
                radial = np.linalg.norm(rays[:,:,:2]-obstacle['xy'], axis=2)
                hidden &= ((radial < obstacle['radius_m']) & (rays[:,:,2]<obstacle['height_m'])).any(axis=1)
            seen &= ~hidden
        per_cam[cam['id']] = float(geo.mean()) if len(pts) else 0.
        geometric.append(geo); visible.append(seen)
    any_geo = np.any(geometric, axis=0) if cams else np.zeros(len(pts),bool)
    any_vis = np.any(visible, axis=0) if cams else np.zeros(len(pts),bool)
    under = np.zeros(len(pts), bool)
    for obstacle in occluders:
        under |= np.linalg.norm(pts-obstacle['xy'], axis=1)<obstacle.get('body_radius_m', .8)
    return {'area': polygon.area, 'blind_m2': round(float((~any_geo).sum())*step**2,2),
            'occluded_m2': round(float((any_geo & ~any_vis & ~under).sum())*step**2,2), 'per_cam': per_cam}
