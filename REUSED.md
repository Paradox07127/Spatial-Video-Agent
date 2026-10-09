# REUSED: files extracted from D2/D3

This table lists each file in `sva/` that is extracted or rewritten from an old demo. D2 = `sim/demos/claim-pack-v3` (commit `347f9ed` in this repository, ws commit `8922ee3`); D3 = `sim/demos/zone-clear-v3` (commit `1dec189` in this repository). The old demos are read-only; do not change them.

Status: `planned` = extraction is planned, and the card has not done it yet; `done` = extracted, and the change summary is filled in. The card that owns the target file updates the related row of this table in its card report (after W0, card D maintains this file).

Checked on: 2026-10-09. Line numbers were checked against each source file in `sim/` of this repository. No implementation reports from other cards were received, so their rows stay `planned`.

| Target (`sva/`) | Source (path:lines) | Source commit | Card | Status | Change summary |
|---|---|---|---|---|---|
| `app/tests/contract/test_projection.py` `three_project` | D3 `tests/test_replay.py:11-17` `three_project` | `1dec189` | W0 | done | The original formula supports only a centered principal point and 16:9; changed to `setViewOffset(w, h, w/2−cx, h/2−cy, w, h)` and `aspect=(w·fy)/(h·fx)` from `K`; the reference implementation changed from `zc.Cam.project` to `cv2.projectPoints` |
| `app/core/geometry.py` `ground_point`, `distance` | D3 `app/zc.py:51-64,67-140` (`fit_homography`, `Cam.project`, `Cam.ground`) | `1dec189` | A5 | planned | — |
| `app/core/geometry.py` `coverage` | D3 `app/zc.py:358-387` `occluded`, `coverage` | `1dec189` | A5 | planned | — |
| `app/core/policy.py` `plan_next` | D3 `app/engine.py:239-305` `evaluate_moment` (VOI camera selection) | `1dec189` | A5 | planned | — |
| `app/core/geo.py` `context`, `facades` | D2 `geo.py:83-265` (`_osm`, `_seg_dist`, `_facades`, `_named`, `context`; `maxspeed` at `:198`) | `347f9ed` | A5 | planned | — |
| `app/core/geo.py` `undistort_fisheye` | D2 `geo.py:126-154` (`_camera`, `_project`; `undistortPoints` of `_facades` at `:153`) | `347f9ed` | A5 | planned | — |
| `app/static/warehouse/stage3d.js` | D3 `static/replay3d.js:122-182,185-208,279-300,344-347` (frustums, frame textures, camera views, `project`) | `1dec189` | A3 | planned | — |
| `app/scripts/vendor.py` | D3 `deploy/vendor.py:21-32` `fetch` | `1dec189` | D | done | Keeps download → SHA-256 check → atomic replace through a temporary file; the file list moved to the W0 JSON manifest, 4 parallel threads, `LIB_DIR` target; added an offline `--check` that checks the manifest and the existing files |
| `app/core/trace.py` Weave sub-op | D3 `app/engine.py:22-35` `traced` | `1dec189` | A1 | planned | — |
