"""§6.2: cam2world (OpenGL, column-major) + K drive the three.js camera with no extra flip; it matches cv2.projectPoints < 0.05 px.

three_project mirrors PerspectiveCamera.updateProjectionMatrix with
  camera.matrixWorld.fromArray(cam2world); fov = fov_deg; aspect = (w*fy)/(h*fx);
  camera.setViewOffset(w, h, w/2 - cx, h/2 - cy, w, h)
which is what stage3d.js must do.
"""
import math

import cv2
import numpy as np
import pytest

from .conftest import FL_KEY, WH_KEY, load


def three_project(cam: dict, X: np.ndarray, extra: np.ndarray | None = None) -> np.ndarray:
    M = np.array(cam["cam2world"]).reshape(4, 4, order="F")
    if extra is not None:
        M = M @ extra
    w, h = cam["w"], cam["h"]
    fx, fy, cx, cy = cam["K"]
    near = 0.1
    top = near * math.tan(math.radians(cam["fov_deg"]) / 2)
    height = 2 * top
    width = (w * fy) / (h * fx) * height
    left = -0.5 * width
    off_x, off_y = w / 2 - cx, h / 2 - cy
    left += off_x * width / w
    top -= off_y * height / h
    p = (np.linalg.inv(M) @ np.c_[X, np.ones(len(X))].T).T
    xn, yn = near * p[:, 0] / -p[:, 2], near * p[:, 1] / -p[:, 2]
    x_ndc = (2 * xn - (2 * left + width)) / width
    y_ndc = (2 * yn - (2 * top - height)) / height
    return np.c_[(x_ndc + 1) / 2 * w, (1 - y_ndc) / 2 * h]


def cv_project(cam: dict, X: np.ndarray) -> np.ndarray:
    """Independent reference: OpenCV camera = OpenGL camera with y and z negated."""
    M = np.array(cam["cam2world"]).reshape(4, 4, order="F")
    w2c = np.linalg.inv(M)
    D = np.diag([1.0, -1.0, -1.0])
    R, t = D @ w2c[:3, :3], D @ w2c[:3, 3]
    fx, fy, cx, cy = cam["K"]
    Kmat = np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1.0]])
    uv, _ = cv2.projectPoints(X.astype(np.float64), cv2.Rodrigues(R)[0], t, Kmat, np.zeros(5))
    return uv.reshape(-1, 2)


def ground_points_in_view(cam: dict) -> np.ndarray:
    g = np.array([[x, y, 0.0] for x in np.arange(-8, 8.01, 0.5) for y in np.arange(-5, 5.01, 0.5)])
    uv = cv_project(cam, g)
    M = np.array(cam["cam2world"]).reshape(4, 4, order="F")
    z = (np.linalg.inv(M) @ np.c_[g, np.ones(len(g))].T).T[:, 2]
    keep = (z < -0.1) & (uv[:, 0] >= 0) & (uv[:, 0] < cam["w"]) & (uv[:, 1] >= 0) & (uv[:, 1] < cam["h"])
    return g[keep]


@pytest.mark.parametrize("cam_id", ["ceiling_00", "ceiling_04", "eye_00"])
def test_three_matches_reference(cam_id):
    cam = next(c for c in load(f"sva-out/{WH_KEY}/manifest.json")["cams"] if c["id"] == cam_id)
    X = ground_points_in_view(cam)
    assert len(X) > 20
    err = np.abs(three_project(cam, X) - cv_project(cam, X)).max()
    print(f"{cam_id}: {len(X)} ground points, max |three - cv2| = {err:.2e} px")
    assert err < 0.05


def test_extra_flip_is_caught():
    cam = load(f"sva-out/{WH_KEY}/manifest.json")["cams"][0]
    X = ground_points_in_view(cam)
    flipped = three_project(cam, X, extra=np.diag([1.0, -1.0, -1.0, 1.0]))
    assert np.abs(flipped - cv_project(cam, X)).max() > 50


def test_fixture_exercises_offset_principal_point():
    cams = load(f"sva-out/{WH_KEY}/manifest.json")["cams"]
    assert any((c["K"][2], c["K"][3]) != (c["w"] / 2, c["h"] / 2) for c in cams)
    assert any(c["K"][0] != c["K"][1] for c in cams)


def test_fleet_camera_looks_forward():
    """Fleet cam2world is camera -> ego (x forward, y left, z up): the optical axis (-Z) points along ego +x."""
    cam = load(f"sva-out/{FL_KEY}/manifest.json")["cams"][0]
    M = np.array(cam["cam2world"]).reshape(4, 4, order="F")
    axis = -M[:3, 2]
    assert axis[0] > 0.99 and abs(axis[1]) < 1e-6 and axis[2] < 0
