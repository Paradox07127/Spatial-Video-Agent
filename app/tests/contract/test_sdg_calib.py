"""SDG camera_params -> contract camera: the run's own projection matrix and our K + cam2world project points identically."""
import numpy as np

from pipeline.warehouse import sdg

# ceiling_00 of 001e53453441935632ae_run_1_seed_1288693302, frame 0 (ws raw/sdg_warehouse)
P = {"cameraApertureOffset": [0.0, 0.0], "renderProductResolution": [1920, 1080],
     "cameraProjection": [1.732050852618065, 0.0, 0.0, 0.0, 0.0, 3.0792015157654484, 0.0, 0.0, 0.0, 0.0, 9.999999786482582e-10, -1.0,
                          0.0, 0.0, 0.009999999786482581, 0.0],
     "cameraViewTransform": [0.8753872065126302, -0.24164321518200899, 0.41869534894779403, 0.0, 0.48342242260161383, 0.4375704424960996,
                             -0.758178652548167, 0.0, 1.8173957366807155e-08, 0.8661066126120207, 0.4998593157976859, -0.0,
                             2.581201474807258e-07, -8.436962239954935e-07, -10.002813014077816, 1.0000000000000002]}


def omniverse_project(p, X):
    """Row-vector pipeline as Omniverse renders: clip = [x y z 1] V P, then NDC -> pixels."""
    V = np.array(p["cameraViewTransform"]).reshape(4, 4)
    Pr = np.array(p["cameraProjection"]).reshape(4, 4)
    clip = np.c_[X, np.ones(len(X))] @ V @ Pr
    ndc = clip[:, :2] / clip[:, 3:4]
    w, h = p["renderProductResolution"]
    return np.c_[(ndc[:, 0] + 1) / 2 * w, (1 - ndc[:, 1]) / 2 * h]


def test_scene_id():
    assert sdg.scene_id("001e53453441935632ae_run_1_seed_1288693302") == "wh/sdg/001e5345_run_1"


def test_cam_entry_matches_omniverse_projection():
    cam = sdg.cam_entry(P, "ceiling_00", video=None, thumbs=None, video_w=960, video_h=540)
    M = np.array(cam["cam2world"]).reshape(4, 4, order="F")
    center = M[:3, 3]
    assert center[2] > 2, "ceiling camera is above the floor (Z up)"
    X = np.array([[x, y, 0.0] for x in np.linspace(-4, 4, 9) for y in np.linspace(-4, 4, 9)])
    ref, ours = omniverse_project(P, X), sdg.project(cam, X)
    inside = (ref[:, 0] > 0) & (ref[:, 0] < 1920) & (ref[:, 1] > 0) & (ref[:, 1] < 1080)
    assert inside.sum() > 20
    assert np.abs(ref[inside] - ours[inside]).max() < 0.05
