# Spatial Video Agent (sva)

Spatial Video Agent is an investigation workbench for multi-camera warehouse surveillance and fleet dashcams. You ask a question in one sentence. The agent finds the moment to one-second precision, gets more camera views when it needs them, and shows the evidence in 3D or on a map.

Numbers come only from CV and geometry (distances, positions, visibility, speed). Models parse the question, write captions and narrate. The agent marks each claim SUPPORTED, CONTRADICTED or UNRESOLVED, and it abstains when the scene cannot answer the question.

## Deployed version

Checked on: 2026-10-09.

| Item | Value |
|---|---|
| URL | `http://<team ingress host>/app/` (the VAST Builders Challenge cluster, team namespace) |
| Source | This repository at its first commit. The running pod reports `sva/source: master-6e2832a` (the commit in the development repository). The code is byte-identical; this commit adds the documentation and `.gitignore` changes only. |
| Mode | `MODE=site`, `DATA_BACKEND=vastdb`, `PUBLIC_BASE_PATH=/app` |
| Delivery | `python:3.12-slim` + split source ConfigMaps (section 6 of [the deployment guide](app/scripts/deploy_site.md)) |
| `/app/health` | `ok: true`, `libs_ready: true`, `assets_ok: true`; VastDB rows: clips 4653, detections 1297456, tracks 332, events 1254, gps 60632, places 5378, scene_manifest 146, vss_collection 3315 |
| Workspaces | Warehouse: 80 scenes (79 L3, 1 L2). Fleet: 53 trips (L2). |

Evidence levels:

- **L3**: synthetic warehouse scenes (NVIDIA SDG) with calibrated cameras, depth and ground truth. Metric distances, world positions and blind spots.
- **L2**: real scenes with partial geometry. SmartSpaces warehouse cameras (ground position, not metric) and PIE dashcam trips (GPS, OBD speed, ego-frame distance, building facades).
- **L1**: street video with only 2D detections and captions. Spatial questions end UNRESOLVED (`no_geometry`). The fixtures include one L1 scene; the deployed corpus has none.

[CONTRACT.md](CONTRACT.md) §8.5 gives the full table of what each level can answer.

## Repository layout

| Path | Contents |
|---|---|
| `app/main.py`, `app/api/` | FastAPI app: health, workspaces and scenes, assets, search, fleet, actions, agent (`POST /api/agent/ask` streams SSE) |
| `app/core/` | Agent tools and policy, geometry, store (DuckDB or VastDB), VSS client, Cosmos VLM, embeddings, LLM, Weave trace, query cache |
| `app/static/` | Browser UI without a build step: home, warehouse 3D stage (three.js), fleet map (MapLibre + PMTiles), run page |
| `app/scripts/` | `bootstrap.py` (pod entry point), `vendor.py` + `vendor_manifest.json` (browser libraries pinned by SHA-256), `configmap_size.sh`, `gate.sh`, `deploy_site.md` |
| `app/tests/` | `unit/`, `contract/` (pytest) and `e2e/` (Playwright) |
| `pipeline/` | Offline pipeline: `warehouse/`, `fleet/`, `street/`, `map/`, `common/` (schema check, publish, upload, index build, site map), `eval/` |
| `schemas/` | JSON Schemas of the artifacts, SSE events and API bodies; SQL of the index and app tables |
| `fixtures/` | Small synthetic scenes and the generator `gen_fixture.py`, used by the tests and by dev mode |

The real scene artifacts (`sva-out/`), the index and the source videos are not in Git. Dev mode and all tests use only `fixtures/`.

## Development run

Run all commands from the repository root. Python 3.12. `app/requirements.txt` pins the dependencies.

```bash
uv venv -p 3.12 .venv
VIRTUAL_ENV=.venv uv pip install -r app/requirements.txt
.venv/bin/python app/scripts/vendor.py
.venv/bin/python app/scripts/vendor.py --check
```

Start the app on the synthetic fixtures:

```bash
.venv/bin/python fixtures/gen_fixture.py --no-media --index index.duckdb
MODE=dev DATA_BACKEND=duckdb ASSET_DIR=fixtures/sva-out INDEX_DUCKDB=index.duckdb \
APP_DUCKDB=app.duckdb RUNS_DIR=runs LIB_DIR=app/static/lib PUBLIC_BASE_PATH= PORT=8300 \
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8300
```

Open `http://127.0.0.1:8300/`. The question parser uses deterministic templates first; the LLM classifies only questions without a template keyword (`app/core/llm.py`). The pseudo-vectors of the fixtures are for contract and page tests only; they are not evidence of retrieval accuracy. The environment variables for the model services, VSS, S3, VastDB and W&B are listed in [CONTRACT.md §6.3](CONTRACT.md#63-environment-variables-spec-122). The code never prints the value of a secret.

## Tests

```bash
(cd app && npm install && npx playwright install chromium)
bash app/scripts/gate.sh
```

`gate.sh` runs all pytest suites, the fixture schema check, the fixture determinism check, the vendor manifest check, the Playwright e2e (root path and `/app` prefix), the ConfigMap size check and a secret scan. Set `SVA_E2E_PORT` to give a parallel run its own port.

## Pipeline

The GPU pipeline produces the scene artifacts offline: warehouse depth points, visibility, pose and triangulation, forklift and events; fleet trip selection, detection and tracking, GPS map matching, building facades; then Cosmos records, the index and the schema check. Each step writes to `sva-out/_tmp/<card>/<scene_key>/`. Only `pipeline/common/publish.py` moves a scene that passes `schema_check.py` to `sva-out/<scene_key>/`. `pipeline/common/upload.py` copies the artifacts to S3, and `pipeline/common/index_build.py --vastdb` writes the index tables to VastDB. `pipeline/requirements.txt` lists the pipeline dependencies (torch needs the CUDA wheel index). The input data sets are not in this repository.

## Site deployment

[app/scripts/deploy_site.md](app/scripts/deploy_site.md) gives the steps for the workshop VM: Secret, bootstrap ConfigMap, Deployment + Service + Ingress at `/app` on the team host, and the health and SSE checks.

[REUSED.md](REUSED.md) lists the files taken from earlier demos.
