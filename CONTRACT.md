# sva contract (W0)

This document is a code-level extract of `docs/spatial-video-agent-build-plan.md` v2.3 §6–§10. It also records the decisions made when W0 was implemented (§12). Workers read this document before they start. The executable contract is in `schemas/`, `app/core/models.py`, the signatures of the three stub modules and `fixtures/`. If this document conflicts with them, the executable version applies; point out the conflict in your card report.

Contract changes: only the main session changes `schemas/`, `app/core/models.py`, stub signatures and `fixtures/`. If a card needs a new field or a new signature, it writes this in the "Changes needed from other cards" section of its card report.

Status: approved by the user on 2026-10-09 (including all deviations in §12). W0 additions after approval: the main session implements `lease.py` and `publish.py` (§10); `schema_check.py --file`.

---

## 1. Environment and commands (run all of them in `sva/`)

```bash
uv sync                                                                             # app venv
(cd app && npm install && npx playwright install chromium)                          # e2e
.venv/bin/python -m pytest app/tests/contract -q                                    # contract tests
.venv/bin/python pipeline/common/schema_check.py fixtures/                          # artifact check (also for ws artifacts)
.venv/bin/python fixtures/gen_fixture.py [--no-media] [--index PATH]                # regenerate fixtures
(cd app && npx playwright test -c tests/playwright.config.js [spec])               # e2e (two projects: root and /app)
bash app/scripts/gate.sh --w0 | bash app/scripts/gate.sh                           # W0 gate | integration gate
```

- `pytest.ini` adds `sva/` to `sys.path`: app code uses `from app.core.models import ...`, pipeline code uses `from pipeline.common import ...`.
- The pipeline venv is on ws: `~/sva/.venv`, a symlink to `pipeline/.venv`. Build it with `uv sync --project pipeline` (`pipeline/pyproject.toml` + `pipeline/uv.lock`).

## 2. Directories and ownership (spec §5, §14.3)

Only one card can change each path. Write changes outside your own paths into your report.

| Card | Owns |
|---|---|
| W0 (main session) | `CONTRACT.md` `schemas/*` `fixtures/*` (except `labels/`) `app/core/models.py` `app/tests/contract/*` `app/tests/{playwright.config.js,prefix_proxy.py}` `app/tests/e2e/mockapi.js` `app/package.json` `app/scripts/gate.sh` `pytest.ini`; stub signatures: `app/core/{store,trace,vlm}.py`, `app/api/{search,agent,actions}.py`; implemented: `pipeline/common/{lease,publish,video}.py`, first version of `pipeline/warehouse/sdg.py` |
| A1 | `app/main.py settings.py api/{health,scenes,fleet,assets,actions}.py core/{store,cache,trace}.py tests/unit/test_{store,trace,actions,api}.py app/requirements.txt` |
| A2 | `app/static/{index.html,app.js,styles.css,icons.svg} static/kernel/* tests/e2e/kernel.spec.js` |
| A3 | `app/static/warehouse/* tests/e2e/warehouse.spec.js` |
| A4 | `app/static/fleet/* tests/e2e/fleet.spec.js` |
| A5 | `app/api/{search,agent}.py`, `app/core/{llm,embed,geo,geometry,policy,tools,vss}.py tests/unit/test_{search,policy,geometry,tools,vss,agent}.py tests/unit/a5_fakes.py` |
| P-X | `app/core/vlm.py pipeline/common/cosmos_batch.py pipeline/eval/cosmos_*.py fixtures/labels/* tests/unit/test_vlm.py` |
| P-W1 | `pipeline/warehouse/{sdg,fetch_sdg,depth_points,visibility}.py pipeline/eval/wh_visibility_check.py pipeline/env.sh` |
| P-W2 | `pipeline/warehouse/{pose2d,triangulate,forklift,events,smartspaces,run_all}.py pipeline/eval/{wh_position_error,wh_reprojection}.py pipeline/manifests/scenes.yaml` |
| P-F | `pipeline/fleet/* pipeline/common/detect_track.py pipeline/eval/fl_ground_error.py pipeline/manifests/trips.yaml` |
| P-C | `pipeline/common/{schema_check,embed,caption,index_build,upload,site_map}.py pipeline/common/reingest_prompt.md pipeline/eval/{retrieval_recall,latency,ui_audit,weave_publish}.py pipeline/manifests/queries.yaml` |
| D | `app/scripts/{bootstrap.py,vendor.py,vendor_manifest.json,configmap_size.sh,deploy_site.md} README.md REUSED.md tests/unit/test_deploy.py` |
| T2 (L1 data) | `pipeline/street/*` (`__init__.py`, `l1_scenes.py`, `test_l1_scenes.py`), `pipeline/common/{site_map,index_build}.py` and their tests |
| T3 (L1 data API, retrieval) | `app/core/street.py`, `app/core/{store,vss}.py`, `app/api/{search,scenes}.py`, `app/tests/unit/test_{store,search,api,vss,street}.py`, `app/tests/unit/a5_fakes.py` |
| T4 (agent per level) | `app/api/agent.py`, `app/core/{llm,tools,vlm}.py`, `app/tests/unit/test_{agent,llm,tools,vlm}.py` |

The T rows (L1 wave, 2026-10-09) take the files they list over from A1, A5, P-C and P-X for that wave. `app/static/**` and `app/tests/e2e/**` belong to the UI redesign session.

P-C extends `schema_check.py`, but it must not remove the W0 checks (§4.3). `REUSED.md`: the card that owns the target file gives the row content in its report, and D merges it.

## 3. IDs, coordinates and time (spec §6.1–§6.2)

- **scene_id** has the hierarchical forms `wh/sdg/<run>`, `wh/ss/warehouse_017` and `fl/pie/<video_id>`. On ws, the SDG directory name is `<hash20>_run_<n>_seed_<s>`, and `run_<n>` in it is not unique, so `<run>` is `<first 8 chars of hash20>_run_<n>`, e.g. `wh/sdg/001e5345_run_1`. **scene_key** = `scene_id.replace("/", "-").lower()`, and it contains only `[a-z0-9_-]`. Routes, directory names and file names always use scene_key. The manifest writes both, and `schema_check` enforces this mapping. The fleet `trip_id` = the scene_key of that trip.
- **Street scenes (workspace `street`)**: scene_id `st/<camera_slug>/<video_stem>`, scene_key `st-<camera_slug>-<video_stem>` (lowercased by the rule above), e.g. `st/sf_streets_cam_4/sf4` → `st-sf_streets_cam_4-sf4`. `camera_slug` = VSS `camera_id` lowercased with `-` replaced by `_`; `cam_id` = VSS `camera_id` lowercased (`sf_streets_cam-4`); `video_stem` = the VSS original file name without the upload-time prefix and `_chunk_NNNN.mp4` (`sf4`, `VID_20261006_174811_003`, `IMG_0509`, `neighborhood_20260901`; the street scene_id may keep its case, the scene_key never does). One scene per source video. Street sources: `nyc_streets_cam-1`, `nyc_streets_cam-2`, `sf_streets_cam-1..5`, `neighborhood_cam-1`; `i24_cam-1` stays unmapped.
- **Prefix rule**: the scene_key prefix matches the workspace: `wh-` warehouse, `fl-` fleet, `st-` street (`schema_check`). The app reads the workspace of a key from the manifest or `/api/workspaces`, never from the prefix.
- **Evidence level** (`models.level_of(manifest) -> Level`): the manifest's optional `evidence_level` if set; else street → `L1` (one 2D camera, no geometry, no map, no distance); warehouse with `metric=true` → `L3`; everything else (SmartSpaces `metric=false`, PIE trips) → `L2`.
- **Capabilities** (`models.capabilities(manifest) -> {cap: {ok, reason, text}}`, caps in `models.CAPABILITIES` order: `distance ground_position map blind_spot tracks speed boxes_2d caption cosmos`). Reasons and texts come only from `models.ABSTAIN_TEXT`; they are the single source of the agent's abstentions and the UI's "cannot prove" lines:

| reason | when | text |
|---|---|---|
| `no_geometry` | street: distance, ground_position, map, blind_spot | This camera has no geometry, so distances, speeds and positions are not judged. |
| `scene_not_metric` | distance when `metric=false` (not street) | This scene's calibration is not metric, so meters are not given. |
| `no_track_ids` | street: tracks | Detections on this camera have no track ids, so one person cannot be followed across frames. |
| `no_gps` | speed unless fleet with a `trip` asset | This camera has no GPS or speed data. |
| `no_visibility` | blind_spot without a `visibility` asset (not street) | This scene has no camera visibility grid, so blind spots are not checked. |

`boxes_2d`, `caption`, `cosmos` are ok when the scene has cameras (reason null otherwise).

- **Entity**: `EntityRef = {scene_key, entity_id, kind}`. `entity_id` has the form `P3` (person), `F1` (forklift), `V7` (vehicle). **Observation**: `ObservationRef = {scene_key, cam_id, local_track_id, t_ms}`. `boxes` stores only the camera-local `local_track_id`. `entities.json` is the only observation → entity mapping. All other files, tool arguments and `selection` use `entity_id`.
- **Warehouse world frame**: meters, Z up, ground at z=0. The forklift `heading` is in radians, counterclockwise from world +x.
- **Camera**: `cam2world` is 16 numbers of a 4×4 **column-major** matrix in the OpenGL convention (camera +X right, +Y up, looking along −Z). For fleet, `cam2world` is camera → ego frame. `K=[fx,fy,cx,cy]`, in source pixels (`w×h`). Do not assume that the principal point is centered. `fx` can differ from `fy`. `fov_deg = 2·atan(h/(2·fy))` (vertical). **three.js rule**: `camera.matrixWorld.fromArray(cam2world)` (do not multiply by a flip matrix), `camera.fov = fov_deg`, `camera.aspect = (w·fy)/(h·fx)`, `camera.setViewOffset(w, h, w/2 − cx, h/2 − cy, w, h)`, `up = (0,0,1)`. The contract test `test_projection.py` uses the same formula and checks it against `cv2.projectPoints` to < 0.05 px.
- **Fisheye**: `model:"fisheye"`, `dist=[k1,k2,k3,k4]`, the OpenCV `cv2.fisheye` model.
- **Fleet**: the map is WGS84, with coordinate order `[lon, lat]`. `heading` is in degrees, clockwise from true north. `speed` is in m/s. Ego frame: x forward, y left, z up (meters). Vehicle `yaw` is in radians, counterclockwise from ego +x. D2 `Ground.point` returns (right, forward); A5 converts it to (forward, left) in `geo.py`.
- **Time**: `t_ms` is integer milliseconds relative to the scene `t0_ms` (always 0). Intervals are half-open `[t0_ms, t1_ms)`. For each camera, `t_scene_ms = offset_ms + media_pts_ms`. Three time bases are kept separate: `capture_ms`, `upload_ms` (VSS `time_filter` uses it) and `analyzed_ms`. A scene with `sync_method="unknown"` must not give cross-camera timing conclusions.
- **Fleet replay clock**: `ReplayMapping = {trip_id, replay_start_ms, source_start_ms, rate, coverage_intervals}`. `t_ms = source_start_ms + (wall_ms − replay_start_ms)·rate`, valid only inside `coverage_intervals` (trip t_ms). GPS point `state ∈ observed|interpolated|last_known|unknown`. `max_interpolation_gap_ms = 2000`. A longer gap is not interpolated: points in the gap are `last_known`, their coordinates are the last fix, and `gap_ms` = milliseconds since the last fix.
- **Metric switch**: when `manifest.metric=false` (SmartSpaces), `measure` returns `UNRESOLVED(reason="scene_not_metric")`; closest-approach scans exclude nonmetric scenes and report their skipped coverage. Live projection also abstains before reading sidecars. Track-speed sigma is in m/s, derived from position sigma and sample separation when no speed sigma is supplied.

## 4. Artifacts (spec §6.3)

### 4.1 Common rules

- Each scene has one directory `<ASSET_DIR>/<scene_key>/`. The `assets` of `manifest.json` lists the other files with relative paths.
- **The root of every JSON artifact is an object** with the common header `{schema_version:"1", analysis_rev, producer_commit, input_artifact_hashes:{relpath: "sha256:<hex>"}}` (in `dets2d` only `schema_version` is required, §12 #17). Files whose root is an array in the spec get one wrapper level: `entities.entities[]`, `events.events[]`, `tracks.tracks[]`, `cosmos.records[]`, `boxes.rows[]`, `ego.frames[]`, `forklift.forklifts[]`, `buildings.buildings{}`.
- Chunk file names: `boxes/<cam>/<chunk>.json`, `ego/<chunk>.json`, `<chunk> = f"{chunk_t0_ms // 10000:05d}"`. A chunk holds `t_ms ∈ [chunk_t0_ms, chunk_t0_ms+10000)`.
- Street scenes: `dets/<cam_id>/<chunk_index:04d>.json`, where `chunk_index` indexes `cams[].chunks` (VSS 30 s chunks, not 10 s). Fixture chunk videos: `chunks/<cam_id>/<chunk_index:04d>.mp4`.
- Thumbnails: `thumbs/<cam>/<s:05d>.jpg`, `s = floor(t_ms/1000)`, the first frame in that second. If that second has no video, no thumbnail is made (the frontend shows a gray block). Video: `video/<cam>.mp4` (H.264, GOP 1 s, faststart).

### 4.2 Files and schemas

| File | schema | Key points |
|---|---|---|
| `manifest.json` | `manifest.json` | `cams[]`: `id w h K model dist camera_basis:"opengl" cam2world fov_deg video frame_ref{video_w,video_h,thumb_w,thumb_h} offset_ms thumbs sync_method sync_residual_ms`; warehouse must have `ground{origin,cell_m,nx,ny,cam_bit_order,texture?,texture_from_world?}`; `points_meta{n,voxel_m,depth_kind}`; warehouse `assets` must have entities/timeline/events/tracks/boxes_dir/skeleton/cosmos, fleet must have trip. Optional `evidence_level` (L1, L2, L3) overrides `level_of`. **Street** (`workspace:"street"`, `metric:false`, `assets.dets_dir` required): every camera is the `cam2d` variant `{kind:"cam2d", id, w, h, video, chunks[{t0_ms, t1_ms, uri}], frame_ref, offset_ms, thumbs, sync_method, sync_residual_ms}` with no `K`/`model`/`dist`/`camera_basis`/`cam2world`/`fov_deg`; chunks are sorted and contiguous, chunk i covers `[t0_ms, t1_ms)` of scene time (its stream position), scene `t1_ms` = last chunk end; fixtures: `video`, `thumbs` and `uri` are relative asset paths; site: `video` and `thumbs` null, `uri` = `s3://…` |
| `dets/<cam>/<chunk>.json` | `dets2d.json` | street only: `{schema_version, analysis_rev?, producer_commit?, input_artifact_hashes?, cam_id, chunk_index, chunk_t0_ms, source, rows[[t_ms, label, x1, y1, x2, y2, conf]]}`; `t_ms` scene time inside the chunk; source pixels; any COCO label; no track id; `source` = detector (`yolo11_coco`) |
| `entities.json` | `entities.json` | `entities[]{entity_id, kind, observations[{cam_id, local_track_id, t0_ms, t1_ms, method, conf}], association{method, conf, valid}}` |
| `timeline.json` | `timeline.json` | `sec[[t_s, n_person, n_vehicle, cam_coverage_bits, event_ids]]` (bit i = `cam_bit_order[i]` has video at the midpoint of that second), `lanes{cam:[[t0,t1,"ok"|"occluded"|"no_video"]]}` |
| `events.json` | `events.json` | `events[]{id:"E<n>", type, t0_ms, t1_ms, actor_ids, place_id?, severity 0..3, source rule|gt|model, params, clip_ids, eids}` |
| `boxes/<cam>/<chunk>.json` | `boxes.json` | `rows[[t_ms, local_track_id, cls, x1, y1, x2, y2, conf]]`, source pixels |
| `skeleton.json` | `skeleton.json` | `persons{entity_id:{t_ms[], root_world[], root_method[tri|ankle], root_sigma_m[], xyz[17×xyz|null], conf[], n_views[]}}`; a single-view frame has `xyz=null`; `root_sigma_m` is a per-axis 1-sigma in metres; frames without triangulation or a confident ankle carry no root |
| `points.bin` | (checked by schema_check) | Little-endian; **32 B header**: `"SVAP"`, `u32 N`, `f32 bbox[6]` (minx,miny,minz,maxx,maxy,maxz); then `f32 xyz×N`, `u8 rgb×N`; total length = 32 + 15N |
| `visibility.json` | `visibility.json` | Only means "the ground cell is visible to the camera". `per_sec[s]` = base64 of `nx·ny·n_cams` bytes, byte index `(iy·nx + ix)·n_cams + cam_index`, values: 0 unknown, 1 visible, 2 occluded, 3 out_of_fov, 4 no_video; center of cell `(ix,iy)` = `origin + ((ix+0.5)·cell_m, (iy+0.5)·cell_m)`; second s uses the time `s·1000+500` |
| `forklift.json` | `forklift.json` | `forklifts[]{entity_id, source, track[{t_ms,x,y,heading,speed,footprint[4],sigma_m,source}], gt[{t_ms,x,y,heading}]}`; GT = center of the bbox3d bottom face + local +x |
| `tracks.json` | `tracks.json` | `tracks[]{entity_id, cls, t0_ms, t1_ms, cam_ids, pts[[t_ms, wx, wy, sigma_m, state]], source}` |
| `cosmos/<scene_key>.json` | `cosmos.json` | `records[]{record_id, task C1..C5, t_ms, cam_id, actor_refs, input_frame_refs, prefix_only, model_rev, prompt_rev, payload, raw?, verification{status, method, ref}}`, payload is validated per task (§8.4) |
| `trip.geojson` | `trip.json` | FeatureCollection; `features[0]` = LineString (`properties.replay` = ReplayMapping), followed by 2 Hz Points (`t_ms lat lon heading speed way_id node_id place_id state gap_ms maxspeed_kmh`; `maxspeed_kmh` null means no speed limit data) |
| `ego/<chunk>.json` | `ego.json` | 10 fps `frames[]{t_ms, ego{lat,lon,heading,sigma_m,state}, peds[{entity_id,x,y,h,sigma_m,kpts2d,source}], vehs[{entity_id,x,y,yaw,cls,dims,sigma_m}], buildings[{bid,u0,u1}]}` |
| `buildings.json` | `buildings.json` | `buildings{bid:{name, addr, height_m, floors, pois[], wikidata, osm_id, geom, source, map_snapshot_date}}` |
| `_map/{style.json,basemap.pmtiles,buildings.pmtiles}` | (not a scene, no schema) | Fleet offline map in `<ASSET_DIR>/_map/`, read through `GET /api/assets/_map/...` (Range): OpenMapTiles basemap z0–14, 3D Massing buildings (layer `buildings` z13–16, `massing` z10–12, attributes `bid h`); generated by `pipeline/map/{basemap,buildings}.py` |

### 4.3 Cross-file checks in `schema_check.py` (implemented in W0)

scene_key mapping and directory name; videos and thumbnails exist; fisheye has 4 distortion coefficients; `fov_deg` matches `K`; an observation (cam, local_track_id) belongs to only one entity; every referenced `entity_id` exists in entities; every track in `boxes` has an entity observation; times are inside the scene; events that `timeline` references exist; `visibility` matches the `ground` grid and camera order, and the byte count per second is correct; `skeleton` parallel arrays have equal length, and frames with joints have ≥ 2 views; C2 frames are all in `[t−2000, t)` and `max_frame_ms` = the latest frame time; `points.bin` length; GPS points increase, `interpolated` does not span a gap > 2000 ms, coordinates match attributes; buildings that ego references exist. For the fixtures directory, it also checks `asks.json` (§7), `runs/*.json` and `labels/*.json`. L1 additions: the scene_key prefix matches the workspace (§3); 2D cameras get no fov/fisheye check; their chunks are sorted, non-overlapping, non-empty and inside the scene, and a relative chunk `uri` exists; each dets file matches its path (`cam_id`, `chunk_index`) and `chunks[chunk_index].t0_ms`, and every row lies inside that chunk; the fixtures directory also gets `l1/asks_corpus.json` (same rules as `asks.json`).

## 5. Index (spec §6.4)

- The only source of DDL: `schemas/index.sql` (`index.duckdb`, produced by the pipeline, read-only for the app) and `schemas/app.sql` (`app.duckdb`: `agent_runs`, `query_cache`, `actions`, `zones`, read and written by the app). The VastDB schema `sva` uses tables with the same names.
- IDs: `clip_id = f"{scene_key}:{cam_id}:{t0_ms // 5000:05d}"` (5 s clip); `event_id = f"{scene_key}:{id in events.json}"`; `gtrack_id = f"{scene_key}:{entity_id}"`.
- Source tags become scene filters before ranking, top-k and level quotas. Explicit camera ids in questions become camera filters; a retrieved moment uses the camera that supplied its verified participant observations. Event timestamps require a participant observation inside the event and requested interval, within 250 ms of the candidate time; absent samples produce `no_participant_sample` gaps. Zone predicates check event or requested actors, and zone assessments record `params.zone_t_ms` inside the event/request intersection.
- Thumbnail URLs use manifest paths and require an existing asset; absent thumbnails return null and `missing_thumbnail` gaps without decoding frames. Street cache files and VLM cache records use unique temporary files and atomic replacement.
- Street cards, detections, localization input and evidence are clipped to the requested interval. Detection coverage is the union of available frame spans; gaps are clipped, merged and include intervals without indexed segments or frames.
- The time filter uses interval intersection `t0_ms < :q_end AND t1_ms > :q_start`. Text and visual retrieval each take the top 50. The results are merged by `clip_id`, and `rank_text`, `rank_visual` and the raw similarity are kept. Fusion is RRF `Σ 1/(60 + rank)`. Hard conditions (scene, time, place) filter first and do not depend on similarity.
- Street clips: one clip per 5 s VSS segment per camera, `workspace='street'`, `video_uri` = the chunk uri, `vss_source` = the segment S3 uri, `caption` from the live row, `n_person`/`n_vehicle` NULL (not 0) when no boxes were counted (the fixture counts the most boxes of a kind in one frame). Street detections stay in `dets/`, not in the `detections` table.
- `array_cosine_similarity(FLOAT[256], FLOAT[256])` of DuckDB 1.5.6 works in this venv (tested in `test_duckdb.py`).
- `DATA_BACKEND=duckdb|vastdb` and `MODE=dev|site` are separate. By default, site does not use DuckDB. It falls back to DuckDB only when `ALLOW_DUCKDB_FALLBACK=1`.

## 6. Backend (spec §7)

### 6.1 Shared models and stubs

- `app/core/models.py`: `EntityRef ObservationRef Evidence Envelope ToolResult MomentCard Predicate Claim CosmosRecord Query SearchFilters Clip Event Moment Entity RunStep AgentRun Action Zone`, one-to-one with `schemas/`; `make_eid`, `make_record_id`, `aggregate_of`, `scene_key_of`. L1 wave: `Workspace = Literal["warehouse","fleet","street"]`, `Level = Literal["L1","L2","L3"]`, `Scope = Literal["scene","corpus"]`; `level_of(manifest: dict) -> Level`, `capabilities(manifest: dict) -> dict[str, dict]`, `ABSTAIN_TEXT: dict[str, str]`, `CAPABILITIES` (§3); `Query.scope: Scope = "scene"`; optional `Evidence.evidence_level` (not in the eid), `MomentCard.workspace/evidence_level` and `Claim.scene_key/moment_id/evidence_level` (`Claim.explain` may hold `abstain`). Source tags: `Tag = Literal["warehouse","fleet","fixed"]`, `tag_of(manifest: dict) -> Tag`; `Query.sources: list[Tag] | None = None`; optional `MomentCard.tag/title`.
- `app/core/store.py` (A1 implements): `Store(*, index_path, app_path, asset_dir, backend, allow_duckdb_fallback)`; index `search_clips events_in moment entities index_counts`; assets `manifest asset_json asset_url list_scenes`; trace `begin_run append_step update_step add_evidence set_hits finalize_run get_run list_runs`; cache `cache_get cache_put`; actions `find_action insert_action list_actions insert_zone list_zones`; `write_vastdb`. All are synchronous.
- `app/core/trace.py` (A1 implements, A5 calls): `start_run(store, q, context, *, kind, source_run_id) -> RunHandle`; `with traced(run, name, inputs, *, state, parent_step_id) as t:` `t.output(x)`, `t.evidence(eids)`, `t.note(s)`, `t.exec_state(s)`; `finish_run(run, status, *, final, claims) -> weave_url`; `flush_outbox()`. A step is **written locally before execution continues**. Inputs and outputs > 8 KB go to `RUNS_DIR/<run_id>/step_<i>.json`, and the row stores `blob_ref{uri, sha256, size}`. Weave failures go to the outbox. The local trace is never skipped. `RunHandle.read_eids` is the allowlist of eids that the answer can cite.
- `app/core/vlm.py` (P-X implements, A5 calls): `situation predict_next ground_objects localize describe_region`. All of them return `CosmosRecord` and take the keyword argument `run` (when it is given, a `vlm.<task>` substep is recorded). On failure they raise `VLMUnavailable`.
- `app/tests/contract/test_stubs.py` freezes the signatures.

### 6.2 Routes (spec §7.2; routes always use scene_key)

`GET /health` → `{ok, mode, libs_ready, index_rows, assets_ok}`; `GET /api/workspaces`; `GET /api/scene/{scene_key}/manifest|timeline|entities`; `GET /api/scene/{scene_key}/moment?t=` → `Moment`; `GET /api/model-results?scene_key=&t_ms=&window_ms=` (A1 reads `cosmos/<scene_key>.json`); `POST /api/scene/{scene_key}/zone`; `GET /api/fleet/trips`; `GET /api/fleet/trip/{trip_id}/moment?t=`; `GET /api/fleet/building/{bid}`; `POST /api/search` (`search.json`); `POST /api/agent/ask` (SSE, §7); `GET /api/agent/runs/{run_id}` (`agent_run.json`); `GET /api/agent/runs?scene_key=&limit=`; `POST /api/agent/confirm`; `POST /api/actions/trigger` (entry function `app.api.actions.trigger(store, TriggerRequest) -> TriggerResult`, A1 implements; the route, the frontend replay trigger and the agent's SUPPORTED near miss all call it; the same `(scene_key, event_id)` triggers only once, and a second call returns the existing `action_id`; the dynamic hazard zone = a polygon 3 m outside the incident point); `GET /api/actions?scene_key=` → `{actions: Action[], zones: Zone[]}` (the zone polygon is `zones[].polygon_world`); `GET /api/assets/{path:path}` (dev: local files, with Range support; site: 302 to a presigned S3 URL); `GET /lib/{path:path}` (`LIB_DIR`).

L1 routes (T3 implements them on the existing `app/api/scenes.py` router, no `main.py` change):
- `GET /api/workspaces` → groups `warehouse`, `fleet`, `street`; every scene also carries `evidence_level` and `caps` (`capabilities(manifest)`).
- `GET /api/scene/{key}/manifest` returns the `cam2d` manifest for a street scene.
- `GET /api/street/{key}/segments` → `[{clip_id, cam_id, t0_ms, t1_ms, caption, counts, source}]`.
- `GET /api/street/{key}/detections?cam=&t0=&t1=` → `{cam_id, rows:[[t_ms, label, x1, y1, x2, y2, conf]], source, gaps}`.
- `GET /api/street/{key}/chunks?cam=` → `[{index, t0_ms, t1_ms, url}]`.
- `GET /api/street/{key}/chunk/{cam}/{index}` → mp4 bytes, honours `Range` (206); site mode proxies S3 (browsers cannot reach the internal S3; no mirror).
- `GET /api/street/{key}/thumb?cam=&t=` → JPEG; site mode decodes one frame from the chunk and caches it under `RUNS_DIR`.

### 6.3 Environment variables (spec §12.2)

`MODE PORT PUBLIC_BASE_PATH DATA_BACKEND ALLOW_DUCKDB_FALLBACK ASSET_DIR INDEX_DUCKDB APP_DUCKDB RUNS_DIR LIB_DIR VSS_URL VSS_USERNAME VSS_PASSWORD S3_ENDPOINT ACCESS_KEY SECRET_KEY ASSET_BUCKET VASTDB_BUCKET VDB_SCHEMA VDB_COLLECTION VDB_ENDPOINT VLM_URL VLM_MODEL EMBED_URL EMBED_MODEL YOLO_URL GPU_BEARER_TOKEN LLM_URL LLM_MODEL_SMALL LLM_MODEL_LARGE WANDB_API_KEY WANDB_TEAM WANDB_PROJECT WEAVE_PROJECT`. For the site mapping, see spec §12.2. Code and logs never print the value of a secret.

### 6.4 Scope and corpus retrieval (T3)

- `context.scope` / `Query.scope` / `SearchRequest.scope` ∈ `scene|corpus`. **Absent → `scene`**: the frozen shell sends no scope and keeps today's behaviour. Forced to `scene` when the question names entity ids (`P2`, `F1` are scene-local), or the context has `t_ms` or `selection`. A `scene_key` in the context alone does not force scene. `workspace` is optional in a search request when `scope` is `corpus` (`search.json` `$defs/request`).
- `retrieve(store, req, parsed=None, vss=None)` with `scope=corpus`: call the unchanged `store.search_clips` once per workspace (keeps `WHERE workspace=?` and k per branch per workspace, so one level cannot crowd out another); at most **4 moments per level**; order **L3, L2, L1, then score**.
- **Source tags** (`models.tag_of`): `warehouse` (SDG, SmartSpaces), `fleet` (PIE trips), `fixed` (all street cameras: NYC street cam-1/2, SF sf1–sf5, neighborhood). `context.sources` / `Query.sources` / `SearchRequest.sources` is a list of tags. **Set → corpus retrieval over the scenes whose tag is in the list** (it overrides `scope`; the per-level quota and order stay); absent → the scope rule above. The entity-id / `t_ms` / `selection` rule still forces scene. The new shell sends `sources: [tag of the current source]` by default; "All" sends the three tags.
- A street moment is one 5 s segment: `cams: [cam]`, `actor_refs: []`, `clip_id` set, `why` names the 2D counts; evidence `caption_embed1_rrf` (kind model) + `yolo_coco_2d` (kind measure, `cam_id` set, no observation ids, value `{counts, frames_read, source}`). Street scenes never call `box_rows` or `store.entities`.

## 7. SSE (spec §7.3, `schemas/ask_sse.json`)

Each frame is `{event, data}`, and `data.run_id` is required. Events: `step tool_call tool_result hits model_result ui claim answer status done`.

Order rules (implemented by `schema_check.check_sse_sequence`; all of `asks.json` passes):
1. One run_id; the first frame is `step(state=parse)`; the last two frames are `status` and `done`; there is exactly one `status` frame; `step.i` strictly increases.
2. A `tool_call` with the same id must come before `tool_result.call_id`. `tool_call.step_id` / `tool_result.step_id` = the step_id of the outer agent step; `call_id` = the RunStep step_id of that tool call. step_id has the form `<run_id>.s<i>`.
3. `read_eids` = the eids in the `tool_result.evidence` frames seen so far. `hits.moments[].evidence_ids`, `claim.predicates[].eids`, `answer.cites` and `answer.measure_refs[].eid` must be ⊆ `read_eids`; `answer.claim_ids` must already have appeared.
4. When `status.final ∈ {UNSUPPORTED, NO_MATCH, OUT_OF_RANGE}`, `hits` must not contain moments.
5. `status.stop_reason ∈ enough_evidence|no_new_info|budget_exhausted|abstained|error`.

`Evidence.eid = "e_" + sha256(scene_key, cam_id, t0_ms, t1_ms, sorted observation_ids, analysis_rev, method, params)[:16]` (`models.make_eid`). Evidence is immutable. Numbers in answer sentences come only from the Evidence fields that `measure_refs` point to. Frontend: `ViewState = default|searching|hit|abstained`. `hits` goes to `hit`, and the three abstain states go to `abstained`. A new Ask or a workspace switch calls `abort()` on the old stream, and stale frames are dropped by run_id.

Ask request (`POST /api/agent/ask` body `context`): `context.workspace` may be `warehouse`, `fleet` or `street`; the new field `context.scope` ∈ `scene|corpus` defaults to `scene`, and `context.sources` (list of source tags) selects corpus retrieval over those tags (rules in §6.4). With `scope = "corpus"`, `context.workspace` only marks the page the ask came from; it does not filter retrieval.

L1 additions (all optional in the schemas, so the frozen shell ignores them and `asks.json`/`runs/` stay valid):

| Frame | Change | Rule |
|---|---|---|
| `hits.moments[]` | + `workspace` (warehouse, fleet, street), + `evidence_level` (L1, L2, L3) | sorted L3, L2, L1, then score; street moments: `actor_refs: []`, `cams: [cam]`, `clip_id` set, `why` names the 2D counts |
| `hits` | + `auto_open` (scene_key or null); `moments[]` + `tag`, `title` | `auto_open` = the scene of every moment when all moments are in one scene, else null. The agent sends `seek`/`highlight` ui frames only for the `auto_open` scene; with `auto_open` null it sends no navigation ui frames and the shell shows the candidate list (§9 rule 6) |
| `ui` | + top-level `scene_key` (`ui_cmd.json`) | every agent ui frame carries it; `seek.args.scene_key` stays; street scenes only get `seek` and `annotate`; `highlight` ids are local to `scene_key` |
| `model_result` | none (`scene_key` already required) | street Cosmos C3 boxes are 0–1000 of the frame; C4 segments are in scene seconds |
| `claim` | + `scene_key`, `moment_id`, `evidence_level`; `explain.abstain = {reason, text}` (reason from §3) | a claim with `explain.abstain` does not count toward `status.final` |
| `answer` | + `evidence_level` (null for an overall sentence) | one sentence per level group; numbers only through `measure_refs` (unchanged) |
| `tool_result.evidence[]` | new method `yolo_coco_2d` (kind measure, `cam_id` set); `cosmos_C3`/`cosmos_C4` on street scenes (kind model) | the Evidence tab keeps grouping by kind |
| every evidence object (`tool_result.evidence[]`, `agent_run.evidence[]`, `$defs/evidence`) | + `evidence_level` (L1, L2, L3, or null) next to `scene_key` = `level_of` of that scene; not part of the eid | the Evidence tab groups by level above the group by kind |
| `status`, `vss_native`, `done` | none | after T2, `vss_native.hits[]` on street sources carry `st-` scene keys |

## 8. Agent (spec §8)

### 8.1 States and tools

Ask context validates the existing workspace, scene_id, scene_key, place, t_ms, t_range, selection, zone, scope and sources fields before streaming. Times are nonnegative integers, ranges have two increasing integer bounds, selections contain strings and zones contain at least three finite numeric coordinate pairs. Workspace may be omitted only with `scope="corpus"`. A supplied scene must belong to the supplied workspace; invalid context returns HTTP 422 before a run starts.

States: `parse → plan → retrieve → inspect → measure → assess → answer`. The LLM is used only in parse and answer. Templates: `find|where_is|what_happened|link`. Outside the templates → `UNSUPPORTED`; time or place outside the footage → `OUT_OF_RANGE`; empty retrieval → `NO_MATCH`; insufficient or conflicting evidence → `UNRESOLVED`. Each tool returns `ToolResult{envelope, evidence}`, `envelope = {input_range, pts_read, model_rev, output, coverage, gaps, quality, elapsed_ms, exec_state, error?}`. Tools: `search get_synced_views visibility measure ground_position ego_distance geocode find_passes events situation(C1) predict_next(C2) ground_objects(C3) localize(C4) describe_region(C5) trigger`. The candidate table `[{cam, gain, reason}]` of `policy.plan_next` is written in full into the output of that step.

`exec_state` records request provenance: `live` when this request calls a model, service or VastDB; `precomputed` for scene asset files and local index snapshots; `cached` for replay; `waiting` for a streamed step before it executes; `failed` for a failed execution. A street sidecar fetched from S3 in this request is `live`; a local sidecar read is `precomputed`. Cosmos scene records are `precomputed`, while replay of the request's model cache is `cached`. A step becomes `live` when one of its calls is live. `trigger` is live only for a newly delivered site alert. Scene-scope investigation assesses at most `policy.MAX_MOMENTS = 3` retrieved moments; corpus quotas and level reservations remain unchanged.

For the first scene-scope fleet hit containing a pedestrian, `ego_distance` calls `live_measure.measure` over the hit's interval. It selects the live value when available, otherwise the closest precomputed pedestrian ahead within 40 m; with neither it returns UNRESOLVED. The output retains both candidates and the live failure reason when falling back. It reports a minimum over valid observed samples; full-window coverage is not certified. Metric capability gates this measurement. This optional measurement runs only without explicit entity or zone constraints because the current tool cannot bind them.

### 8.2 Predicates and aggregation

Within a claim, any `contradicted` → CONTRADICTED; all `supported` → SUPPORTED; otherwise UNRESOLVED (`models.aggregate_of`). Independent candidate claims use the question's quantifier in both scene and corpus scope and in level answers: existential questions need one supported candidate; contradiction requires all candidates contradicted and complete checking. Explicit all/every/each and no/none questions remain UNRESOLVED without an exhaustive entity/time coverage certificate; checking retrieved top-k candidates is not such a certificate. Abstained claims do not count. A Claim records `examined_eids` and `available_eids`. `before`/`after` show the before and after cards. Cosmos descriptions are explanation only; availability does not add proof requirements. A material Cosmos/CV conflict makes the affected requested predicates unknown. Answer sentences cite only the described claim's predicate eids, including its requested event and closest-approach timestamp evidence.

Speeding evidence includes camera-free GPS/OBD trips as map-only cards (`cams=[]`, `primary_cam=""`, `thumb=null`). A known over-limit sample supports an existential speeding claim; contradiction requires full speed/limit coverage of the requested interval across selected trips. With incomplete coverage and no verified speeding, the answer is UNRESOLVED, reports the maximum measured speed with its own map limit for the covered part, and `explain.note` names the unchecked intervals and reasons. The fixture speeding preset is UNRESOLVED with a 4.0-7.0 s GPS gap; other preset statuses are unchanged. Link support requires a matching entity, valid association and actual detection observations in every requested camera or clip, with multiple relevant observations.

Direct warehouse measurements preserve the same time/entity/zone/condition constraints. An explicit person–forklift pair distance without a requested action uses the existing closest scan and reports the minimum observed center distance, its sampled window and timestamp; it does not require a near-miss label. A scalar track-speed request (`how fast`, `speed`) needs one unambiguous entity and a timestamp; it uses `measure(kind="speed")` in m/s, distinct from GPS/road-limit speeding. Missing time, ambiguous targets, unavailable samples or unsupported speed comparisons/zones remain UNRESOLVED with a specific reason. A valid speed with unavailable uncertainty keeps numeric value/time references but no sigma reference and no supported motion condition. Warehouse `where_is` requires a timestamp and one unambiguous entity: `ground_position` emits kind=measure with `value={entity_id,x_m,y_m,t_s,coordinate_frame:"scene_world_ground",source}` and `sigma` in meters, from a valid sampled person root or forklift track. Nonmetric calibration, missing roots and points outside the requested zone abstain; camera visibility alone never supports position. These evidence values use the existing open Evidence.value schema and numeric measure_refs.

Plain near-miss questions require independent rule/GT event evidence. Center distance is a separate quantity and never inherits a footprint-clearance threshold. Ambiguous near-miss comparisons and unsupported footprint/edge/net-clearance requests remain UNRESOLVED; explicit center-distance comparisons remain available.

Metric comparisons preserve `gt`, `ge`, `lt` and `le` and use the measurement's sigma interval; overlapping uncertainty leaves the comparison unknown. Point-time questions (`at 5 s`, `at 5 seconds`, `at second 5`) request the half-open one-second interval starting there. Structured measurement capability reasons appear in `explain.abstain` and the answer using §3 texts. Completed tool results are assessed and current claims published before the budget prevents another call. Runs with failed tools or unavailable model/detection evidence are not cached.

### 8.3 Trace

Each step in `agent_run.json`: `step_id parent_step_id i state name started_ms elapsed_ms input output evidence_ids note exec_state weave_call_id trace_delivery_state blob_ref`. `name` ∈ `parse retrieve plan assess answer policy.plan_next tool.<x> llm.<role> vlm.<task> cache_replay`. A cache hit creates a new run. Its `source_run_id` points to the original run, and the step name is `cache_replay`.

### 8.4 Cosmos records

C1 `{label, reason, uncertainty}`, label ∈ `interaction passing yielding unrelated crossing waiting walking_along`; C2 `{actions ⊆ stop|slow|turn|continue, reason, horizon_s, max_frame_ms, actual?, ghost?}` and `prefix_only=true`; C3 `{objects[{label, box_0_1000, ground_poly, depth_residual_m, geometric_support}]}` (not written into visibility.json); C4 `{query, segments[{start_s,end_s,text}]}`; C5 `{region_ref, text, sign_text}`. The ground-truth rules are in `x-sva-rules` of `schemas/situation_map.json`. The label files are `fixtures/labels/*.json`. Street scenes reuse C3 and C4 with a street prompt revision (`prompt_rev`), so the task enum stays C1–C5.

### 8.5 Agent per evidence level (T4)

- Point-time street follow-ups use the 2D path; camera-free fleet location follow-ups use trip GPS evidence and do not read geometry or entity assets. Missing visibility grids abstain with `no_visibility` while independent measurements continue.
- Per-card dispatch by `level_of` of the card's scene: an L1 card never reaches `measure`, `visibility`, `speed_limit`, `get_synced_views`, `box_rows` or `store.entities`. Special-path gates (blind spot, closest approach: warehouse; building facade, speeding: fleet) read the scene's workspace, not the context's; in corpus scope they scan the scenes of their workspace, and a level that cannot answer the template gets one abstain sentence built from `ABSTAIN_TEXT`.
- L1 card: `ui seek` with `scene_key` (no highlight: no entity ids); tool `detections_2d`; predicate `detected` for the asked COCO classes is supported when present in a read frame, otherwise unknown (a YOLO miss is not a contradiction); a spatial predicate (distance, located, speed) is `unknown` with `claim.explain.abstain = {reason: "no_geometry", text}` and no `measure_refs`; captions are model evidence and never the only support; Cosmos C4 on the top L1 card only.
- `status.final` uses the candidate quantifier and coverage rules in §8.2 over claims without `explain.abstain`; level answers use the same rules; all claims abstained → UNRESOLVED; NO_MATCH only when no level has hits.

| Template | L3 SDG | L2 SmartSpaces (metric false) | L2 PIE | L1 street |
|---|---|---|---|---|
| find / event / presence | events, detections, C1 | same | events, C1 (fleet labels) | 2D presence (`yolo_coco_2d`); caption is model evidence |
| distance / near miss / closest / "close to" | ground distance m ± sigma | UNRESOLVED `scene_not_metric` | ego-frame distance | UNRESOLVED `no_geometry`; reports "in the same frame (2D)" |
| where_is | world position | ground position | GPS place + ego position | UNRESOLVED `no_geometry`, 2D box only |
| blind spot | visibility grid | visibility grid if present (`no_visibility`) | not checked | not checked (`no_geometry`) |
| speeding / building | not checked | not checked | OBD vs map limit / OSM facade | not checked (`no_gps`) |
| link (same person) | entity association | entity association | entity association | UNRESOLVED `no_track_ids` |
| what_happened | events + C1 | events + C1 | events + C1 | caption + Cosmos C4 (model); claim UNRESOLVED |
| identity / intent / blame | UNSUPPORTED | UNSUPPORTED | UNSUPPORTED | UNSUPPORTED |

## 9. Frontend (spec §9)

- The kernel API follows spec §9.1 exactly (`bus clock selection timeline drawer sse state trace modelpanel api`). Page interface: `mount(root, ctx) → dispose`, `page.applyHits / applyUI / applyModelResults / loadScene`. Routes: `#/warehouse/<scene_key>`, `#/fleet`, `#/fleet/<trip_id>@<t_ms>`.
- `api.url(path)` adds `PUBLIC_BASE_PATH` to every URL. Pages use relative paths. The e2e `prefixed` project goes through the `/app/` prefix proxy.
- importmap (`app/scripts/vendor_manifest.json`): `three` → `lib/three/three.module.js`, `three/addons/` → `lib/three/addons/`, `maplibre-gl` → `lib/maplibre/maplibre-gl.mjs` (the worker loads relative to the same directory). three 0.186.0 (OrbitControls, CSS2DRenderer), MapLibre GL 6.13.0. If you need another addon, write it in your report, and D adds it.
- The default state has 0 `<video>` elements. The bottom face of each camera frustum shows the 1 fps images from `thumbs/`. Videos appear only in the hit state.
- e2e: `app/tests/e2e/mockapi.js`: `mockApi(page)` answers `/api/*` from fixtures per §6.2 (including assets, but not Range requests); `mockAsk(page, ask_id)` replays `fixtures/asks.json` as SSE. When there is no `app/main.py`, Playwright starts a static server. Thus page cards do not depend on A1/A5. With `SVA_E2E_LIVE=1`, the mocks are skipped and the tests use the real backend (W2 integration).
- **Routing rules for the redesigned shell (UI session; they remove the cross-page conflicts of the current shell):**
  1. `owns(view, scene_key)`: the mounted view shows that scene (warehouse and street views: exactly one scene; fleet view: any trip). The workspace of a key comes from `/api/workspaces`, never from the prefix.
  2. Every `ui`, `model_result` and measure evidence is queued under its `scene_key` in arrival order and applied now only if `owns(view, scene_key)`. A legacy ui frame without `scene_key` goes only to the scene the ask was sent from.
  3. `hits`: list all level groups; hand a view only the moments it owns.
  4. Opening a hit of another scene or view navigates without aborting the run, then applies that hit and replays its scene queue.
  5. Views drop frames whose `scene_key` they do not own (fleet must not fall back to the open trip); unsupported commands return null.
  6. Candidate list: when `hits.auto_open` is null the shell does not navigate; it lists the moments grouped by `tag`, then `evidence_level`, each with `title`, `thumb`, time, `why` and its claim state ("proves" / "cannot prove" / "retrieved only", updated as claims arrive). The user picks one; opening it follows rule 4. When `auto_open` is set the shell opens that scene as today.
  7. Source tag picker: one chip per tag plus "All"; the default is the tag of the current source (`/api/workspaces` scene → `tag_of`); the chosen tags go to `context.sources`.
- Each hit shows: "proves" = its claim predicates with status and cites; "cannot prove" = the `caps` entries with `ok:false` and their `text`; a hit without a claim = "retrieved only, not inspected". The street view needs chunks + detections + segments and shows the `no_geometry` text as a banner; the default state keeps 0 `<video>` by showing a still frame until play or a hit. The new shell sends `context.sources = [tag of the current source]` by default (§6.4).

## 10. Pipeline (spec §10)

- Use `ssh ws-ts` for ws. Each card checks out its own `~/sva-wt/<card>/`. Artifacts go to `/mnt/data/vast-hack/sva-out/_tmp/<card>/<scene_key>/`. Only `publish.publish()` moves a scene that passes `schema_check` atomically to `sva-out/<scene_key>/`. Only `index_build.py` produces `index.duckdb`.
- Implemented (W0): `publish.publish(card, scene_key)` = the published scene overlaid with the `_tmp` files of that card. A card that adds files to the scene of another card (e.g. the cosmos files of P-X) puts `manifest.patch.json = {assets, models}` in `_tmp` instead of a manifest. The overlay result is swapped in only after it passes `schema_check`. CLI: `python pipeline/common/publish.py CARD SCENE_KEY`. To check a partial artifact alone: `schema_check.py --file visibility.json PATH`.
- Resources (implemented, W0): `lease.acquire(cpu=N, gpu=0|1, owner=card)`. Host totals: CPU 48, GPU pipeline holder 1. Each request to `:8101`/`:8103` uses `lease.vlm_slot(owner=)`, ≤ 8 in total across processes. Set the OMP/MKL/ONNX thread count of each process to the number of CPUs it holds.
- `video.py` (implemented, W0): `probe transcode thumbs sprite frames_at`. The warehouse manifest does not require `cosmos`: P-X adds it with `manifest.patch.json`. P-W2's `run_all.py` assembles the warehouse scenes (it merges the `points.bin`, `visibility.json`, videos, thumbnails and `manifest.part.json` that P-W1 puts in `_tmp/P-W1/<scene_key>/`) and publishes them as P-W2.
- **L1 run order on site**: `pipeline/street/l1_scenes.py` (writes the street manifests and `dets/` from the live VSS rows) → `pipeline/common/site_map.py` → `pipeline/common/upload.py` → `pipeline/common/index_build.py`.

## 11. Fixture (`fixtures/`)

| Scene | Content |
|---|---|
| `wh-sdg-fixture-01` | 3 cameras 640×360, 10 s, 30 fps; P1 and P2 are both present for the whole clip; in `[2000, 2300)` only `ceiling_00` sees P2 (skeleton gives only a single-view ankle root, `root_method=ankle`, `xyz=null`); `eye_00` has no video in `[6000, 7000)` (after that, P1 gets the new local track 14); `ceiling_04` has `offset_ms=120`; the `K` principal point is not centered and fx≠fy; near miss E1 (P1–F1 < 2 m, severity 2) and Dodge E2 (gt); at t≈4600 the forklift blocks the view of `ceiling_00` to the ground cell of P1, and `ceiling_04` and `eye_00` can see it; rack occluders; one record each for C1–C5 |
| `fl-pie-fixture-01` | Fisheye dashcam, 10 s; pedestrian P1 crosses (ped_crossing severity 2), oncoming vehicle V1, hard braking E2; GPS gap (4000, 7000) > 2000 ms → `last_known`; two buildings; C1, C2, C4, C5 |
| `fl-pie-fixture-02` | A 60 s trip with GPS only (overview) |
| `l1/st-fixture_cam_1-clip01` | Street (L1): one `cam2d` camera `fixture_cam-1`, 640×360, 10 s, one chunk `chunks/fixture_cam-1/0000.mp4` (same video as `video/fixture_cam-1.mp4`), thumbnails, `dets/fixture_cam-1/0000.json`; a person walks along the sidewalk for the whole clip, a car passes in `[2000, 8000)`; no geometry, no track ids |

`asks.json`: `wh-main` (UNRESOLVED → get ceiling_04 → SUPPORTED), `wh-refuse` (UNSUPPORTED), `wh-nomatch` (NO_MATCH), `fl-where` (SUPPORTED). `runs/r_fixturewh0001.json` is the full trace of `wh-main`. `gen_fixture.py --index` produces index.duckdb (the vectors are deterministic pseudo-vectors, only for contract and e2e tests).

The street scene lives under `fixtures/l1/`, not `fixtures/sva-out/`, because the frozen e2e mock reads every directory of `sva-out` and knows two workspaces only; the main session moves it into `sva-out/` once the UI session's mock handles any workspace. The default index (`gen_fixture.py --index`, `build_index(path, out)`) is unchanged; `build_index(path, out, l1=True)` adds the street scene for backend tests. `l1/asks_corpus.json` holds corpus asks (`context.scope = "corpus"`): `corpus-anchor` ("Show me every clip, from any camera in any site, where a person is close to a moving vehicle"; hits L3, L2, L1; SUPPORTED), `corpus-street-distance` (L1 only, abstains `no_geometry`; UNRESOLVED), `corpus-nomatch` (NO_MATCH). **Their SSE sequences are placeholders** written by `gen_fixture.py` and checked only by the schema and the §7 order rules; T4 regenerates them from the real agent.

## 12. W0 deviations from and additions to the spec (please approve)

| # | Spec text | W0 implementation | Reason |
|---|---|---|---|
| 1 | Some artifacts have an array root (entities, events, tracks, cosmos, boxes, forklift) | The root is an object, and the array goes into a key with the same name; all JSON files have the common header | The spec also requires each artifact to have `schema_version`, `analysis_rev`, `producer_commit` and `input_artifact_hashes`; an array root cannot hold them |
| 2 | The 32 B header of `points.bin` includes `u32 reserved` | 32 B header = magic + N + bbox[6], no reserved | The original list is actually 36 B; 32 B makes xyz start at a 4-byte boundary, so the frontend can use `Float32Array` directly |
| 3 | `visibility.per_sec` = `u8 × nx·ny`, a 3-bit state per cell | 1 byte per cell per camera, `nx·ny·n_cams` bytes | 3 cameras × 3 bit do not fit in a u8; for a 30×20 m site with 0.5 m cells, 3 cameras for 10 s are about 96 KB, within the 300 KB target |
| 4 | `forklift.json` has a single forklift | `forklifts[]`, each item has `entity_id` | SmartSpaces can have more than one; this agrees with the entity_id contract |
| 5 | Angle units are not defined | Warehouse `heading` in radians (counterclockwise from +x); fleet `heading` in degrees (clockwise from true north); ego frame `yaw` in radians | Prevents a mix of two units |
| 6 | Chunk and thumbnail names, clip_id and event_id are not defined | See §4.1, §5 | Cards must be able to find the files of other cards |
| 7 | eid is made from five items | The first 16 characters of the sha256 of the five items + `method` + `params` | Two different measurements on the same observation would get the same id |
| 8 | The §5 tree has no shared model file | Added `app/core/models.py` (owned by W0) | A1, A5 and P-X all use Evidence, Claim and CosmosRecord; this prevents a separate copy in each card |
| 9 | DDL is only in the spec text | `schemas/index.sql`, `schemas/app.sql`; added columns: `scene_key` in each table, `detections.entity_id`, `gps.state/gap_ms`, `agent_runs.kind/final/evidence/hits/source_run_id`, `query_cache.source_run_id`; unique index on `actions(scene_key, event_id)` | store and index_build read the same DDL; deduplication uses the unique index |
| 10 | `done` has no data; `status` has no stop reason | `done.data = {run_id}`; `status` gets `stop_reason` (and `error`) | The spec requires a run_id in every frame; §8.1 requires the three stop reasons to be recorded separately |
| 11 | Self-check command `npx playwright test tests/e2e/x.spec.js` | The config is in `app/tests/playwright.config.js`, and the command adds `-c tests/playwright.config.js`; `app/package.json` pins `@playwright/test 1.64.0`; the `prefixed` project goes through `app/tests/prefix_proxy.py`, which simulates how the Ingress removes the prefix | The spec puts the config under `tests/`; §13.2 requires a second run under the `/app` prefix |
| 12 | `situation_map.json` is listed as a schema but also looks like mapping data | It is the schema of the label files; the mapping rules are in `x-sva-rules` | Satisfies both uses |
| 13 | Dependency versions are not defined | `pyarrow==23.0.1` (`vastdb 2.1.0` requires ≤ 23.0.1); `opencv-python-headless==4.14.0.94` (not 5.0, to keep the `cv2.fisheye` interface); for the other third-party libraries, see `app/requirements.txt` | Resolved and tested |
| 14 | Store lists only some methods | Added `index_counts manifest asset_json asset_url list_scenes update_step add_evidence set_hits list_runs` and the five action/zone methods; trace is set to `start_run/traced/finish_run/flush_outbox` | A5's `tools.trigger()` and the workflow panel need them; after the signatures are frozen, cards do not wait for each other |
| 15 | e2e depends on the real agent | Page cards replay SSE with `asks.js`; integration with the real backend is in W2 I1 | With 11 cards in parallel, A3/A4 do not wait for A5 |
| 16 | Fixture videos | Commit 4 fixture mp4 files (748 KB in total); `sva/.gitignore` cancels the root `*.mp4` ignore for `fixtures/sva-out/**/*.mp4` | The e2e three-camera video sync needs real videos |
| 17 | (2026-10-09, L1 wave) The spec has two workspaces and no evidence level | Third workspace `street` (scene_id `st/…`, key `st-…`, one 2D `cam2d` camera, `dets/` 2D detections, no geometry, map or distance); `level_of` L1/L2/L3 and `capabilities` with fixed abstain reasons (§3); `scope` scene/corpus, absent = scene; corpus retrieval per workspace, 4 moments per level, order L3, L2, L1, then score (§6.4); optional SSE fields (§7); street routes (§6.2); routing rules for the new shell (§9); L1 run order (§10); street fixture under `fixtures/l1/` with placeholder corpus asks (§11). In `dets2d.json` the common header fields `analysis_rev`, `producer_commit`, `input_artifact_hashes` are optional | The live VSS corpus holds 13 single-camera street videos with captions, vectors and YOLO boxes only; they must be searchable next to L3/L2 scenes without claims they cannot prove. All additions are optional so the frozen shell, `asks.json` and `runs/` stay valid. The header is optional in `dets2d` because the pinned T1–T4 interface writes dets files without it |
| 18 | (2026-10-09, source tags) Corpus retrieval mixes scene types, so a demo ask can jump into an unexpected scene | Source tags `warehouse`/`fleet`/`fixed` (`tag_of`); `context.sources` limits corpus retrieval to those tags, default = the current source's tag; `hits.auto_open` opens a scene only when all moments are in it, otherwise the shell shows a candidate list and the agent sends no navigation ui frames (§6.4, §7, §9 rules 6–7) | User decision 2026-10-09: tags as listed, default one tag, a candidate list across scenes, auto-open for one scene |

### Semantic correctness repair (2026-10-10)

`Query.constraints` retains negation, visual attributes, action modifiers, exhaustive coverage and count/peak requirements; `Query.quantifier` is `any|all|none`. Unmeasured conditions are explicit unknown predicates and `Claim.explain.not_measured` lists them. The LLM fallback preserves these rule-derived conditions. An affirmative event or distance cannot support a negated or universal question. Requested events have their own predicate and require independent event evidence of kind `measure`; a `source=model` event remains model evidence and never creates `measure_refs`. L1 person presence does not prove crossing and carries `no_geometry`. Camera visibility alone does not prove ground position.

Street count and peak bypass ranked retrieval and read the full requested scene/window. `bytetrack_people_count` measures observed ByteTrack trajectories, not physical identities or verified line crossings. `yolo_people_peak` measures maximum simultaneous detections by second and reports the earliest peak frame. Both cite the number and counting window; incomplete input coverage leaves the full-window claim unknown. Frame/SSE event types are unchanged.

`EVIDENCE_REVISION` identifies a published corpus/calibration/sidecar release for answer-cache invalidation. Update it when evidence changes; S3/site deployments must set it explicitly.

`WEBHOOK_URL` optionally enables the existing webhook action channel (five-second timeout, `Idempotency-Key=action_id`). `VDB_VERIFY` defaults to `S3_VERIFY`, or verification enabled when both are unset; VastDB no longer disables TLS verification unconditionally.
# Protocol v2: observations and durable investigations

The default ask protocol remains v1. `/health.agent_protocols` advertises `[1,2]`.
Clients opt into v2 with `protocol_version: 2`; `schemas/ask_sse_v2.json` adds
`observation` events, answer `model_refs`, and measurement `text_spans`.
Offsets are Unicode code points, start-inclusive and end-exclusive. A span is
produced while rendering a measurement placeholder, never guessed by matching
the final numeric text. Visual observations use separate model references and
cannot supply numeric measurements or upgrade a predicate to supported.

Each observation has immutable content identity, scene/camera/half-open input
window, actual frame references, selected subject references, model/prompt/input
identity, and atomic facts with source frames. `verification: unverified` is
mandatory. Model uncertainty is an uncalibrated score. Empty or unavailable
observations preserve unknown; missing video does not authorize a wider input.

`POST /api/agent/sessions` creates a session. GET the collection or one session
to list investigations and read their turns, answers and current-attempt model
observations. A persistent v2 ask supplies `session_id`, `expected_revision`,
`request_id`, and optionally `parent_turn_id`. The same request ID with the same
body returns the same job; a changed body or stale revision returns 409. A parent
must belong to the session and be terminal. Omitted context fields inherit from
that parent; explicit null clears a field. Changing source clears inherited
selection, time and zone. Historical assertions are not treated as newly read
evidence. A source manifest release change requires a new investigation. This
guard hashes declared manifests; it is not an immutable media release registry.
Observation identity additionally hashes decoded input pixels. Replacing assets
in place does not yet preserve historical media bytes; versioned media releases
remain a separate H/D6 work package.

The ask response includes `X-SVA-Session`, `X-SVA-Turn`, and `X-SVA-Job`. SSE `id`
is a job-local increasing sequence number. GET `/api/agent/jobs/{id}` returns
status, attempt, run ID, `last_seq`, and `current_attempt_start_seq`. GET its
`/events?after_seq=N` replays committed frames and follows new ones. A reconnecting
client checks attempt first and replays only the current attempt when it changes.
Disconnecting a subscriber does not cancel execution. POST `/cancel` explicitly
requests cancellation; a blocking model call exits at the next execution boundary,
and no later answer or observation enters the current investigation history or
event stream. The aborted run trace may retain completed-call audit data.
Cancelled/failed jobs may close
without a protocol `done` frame; clients read the job snapshot for the reason.

One process owns the bounded local worker and application database. Requests,
turns, jobs, events and observations commit locally before delivery. Process
recovery retries an unfinished turn in a new attempt with the original remaining
time/tool budget; abandoned in-flight time is conservatively charged. Completed
turns never rerun. This is turn recovery, not instruction-level continuation of a
native model call. Old attempts remain audit history. State and run blobs require
the same persistent volume and a verified clean backup/restore. A background
maintenance loop drains local mirror/blob outboxes without blocking request SQL.

This increment stores observations and investigations. It does not yet implement
the H-plan world-state reducer, prediction ledger, learned observation policy,
human correction propagation, or continuous media ingestion.
