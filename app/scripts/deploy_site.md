# Site deployment (workshop VM)

Updated: 2026-10-11. Historical live-deployment notes below are not a claim of a new site rollout. Sources: the starter kit `deploy-app-no-registry` and spec §12 of the development plan (not in this repository). The app runs at `/app` on the team host in split ConfigMap mode (section 6); the section "Live deployment" below lists how it differs from sections 4–6. The tarball bootstrap accepts this repository’s root layout. Both tarball and split ConfigMap modes pin the accepted source.

On the workshop VM, operate only in the namespace of our team, and use `/app` on the existing host of our team. The internal routes of the app stay at `/`; the Ingress removes `/app`. Artifacts are in the S3 of our team, and structured tables are in the VastDB of our team. Do not redeploy the VSS retrieval stack, do not build or push images, and do not upload new videos.

## 1. Team environment (Prerequisites of the skill)

Run in a **bash** terminal on the VM. The code blocks below share the same shell. Do not turn on `set -x`, and do not print variable values or the Secret YAML. First go to the checked-out project `sva/`.

```bash
set -euo pipefail
set +x
export KUBECONFIG=/config/kubeconfig
kubectl cluster-info
mapfile -t TEAM_CONFIGS < <(find /config -maxdepth 1 -type f -name '*.config' | sort)
(( ${#TEAM_CONFIGS[@]} == 1 )) || { echo 'expected exactly one /config/*.config'; exit 1; }
TEAM_CONFIG="${TEAM_CONFIGS[0]}"
set -a && source "$TEAM_CONFIG" && set +a
NS="$USERNAME"
APP_HOST="${INGRESS_URL#http://}"
APP_HOST="${APP_HOST#https://}"
APP_HOST="${APP_HOST%%/*}"
APP_NAME=sva
APP_PORT=8080
env | cut -d= -f1 | sort
```

If `/config/kubeconfig` does not exist, point to `/config/<team>-k8s.yaml` as the config of our team says. Do not search the configs of other teams. `WANDB_*` can be only in the VM environment and not in the config file. If the namespace is not clear, first ask the site lead to confirm it.

## 2. Pinned source and variable mapping (spec §12.2–§12.3)

The deploy lead enters the public repository name and the full 40-character SHA of the accepted commit, then downloads the same tarball on the VM and computes its hash. The values contain no credentials. The pod must be able to download the source repository without authentication; do not add a token to the URL. For an update, use a new commit and its matching hash.

```bash
read -r -p 'GitHub owner/repository: ' SVA_REPO
read -r -p 'Full SHA of the accepted commit: ' SVA_COMMIT
export SVA_REPO SVA_COMMIT
curl -fL "https://github.com/${SVA_REPO}/archive/${SVA_COMMIT}.tar.gz" -o /tmp/sva-source.tar.gz
SVA_TARBALL_SHA256="$(sha256sum /tmp/sva-source.tar.gz | cut -d' ' -f1)"
export SVA_TARBALL_SHA256
mkdir -p /tmp/sva-release
tar -xzf /tmp/sva-source.tar.gz -C /tmp/sva-release --strip-components=1
cd /tmp/sva-release
test -f app/main.py || { echo 'This commit does not have the A1 app entry point yet'; exit 1; }
```

Each GPU URL uses its own config value. Do not derive one host from another. The VM config has no `COSMOS3_REASON_URL`, `COSMOS_EMBED1_URL`, `YOLO_URL` or `CANARY_1B_URL`, so each one defaults to its host from the gpu skill of the starter kit when unset. The Cosmos3 model id is the one `/v1/models` lists (`nvidia/cosmos3-reason` returns 404). Probe with the health matrix of the gpu skill. If there is a token, send it. Show only the health and the model list.

```bash
COSMOS3_REASON_URL="${COSMOS3_REASON_URL:-http://166.19.38.112:8001}"
COSMOS_EMBED1_URL="${COSMOS_EMBED1_URL:-http://166.19.38.112:8003}"
YOLO_URL="${YOLO_URL:-http://166.19.38.112:8002}"
CANARY_1B_URL="${CANARY_1B_URL:-http://166.19.38.112:8004}"
COSMOS3_REASON_MODEL="${COSMOS3_REASON_MODEL:-nvidia/cosmos3-nano-reasoner}"
COSMOS_EMBED1_MODEL="${COSMOS_EMBED1_MODEL:-nvidia/cosmos-embed1}"
export COSMOS3_REASON_URL COSMOS_EMBED1_URL YOLO_URL CANARY_1B_URL COSMOS3_REASON_MODEL COSMOS_EMBED1_MODEL
GPU_AUTH=()
if [ -n "${GPU_BEARER_TOKEN:-}" ]; then
  GPU_AUTH=(-H "Authorization: Bearer ${GPU_BEARER_TOKEN}")
fi
for MODEL_URL in "$COSMOS3_REASON_URL" "$COSMOS_EMBED1_URL"; do
  curl -fSs "${GPU_AUTH[@]}" "${MODEL_URL%/}/v1/models"
  curl -fSs "${GPU_AUTH[@]}" "${MODEL_URL%/}/v1/health/ready"
  curl -fSs "${GPU_AUTH[@]}" "${MODEL_URL%/}/v1/health/live"
done
curl -fSs "${GPU_AUTH[@]}" "${YOLO_URL%/}/healthz"
curl -fSs "${GPU_AUTH[@]}" "${CANARY_1B_URL%/}/v1/health/ready"
curl -fSs "${GPU_AUTH[@]}" "${CANARY_1B_URL%/}/v1/health/live"
VLM_URL="$COSMOS3_REASON_URL"
VLM_MODEL="$(curl -fSs "${GPU_AUTH[@]}" "${VLM_URL%/}/v1/models" |
  python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["data"][0]["id"] if d.get("data") else "")')"
VLM_MODEL="${VLM_MODEL:-$COSMOS3_REASON_MODEL}"
EMBED_URL="$COSMOS_EMBED1_URL"
EMBED_MODEL="$COSMOS_EMBED1_MODEL"
ASSET_BUCKET="${ASSET_BUCKET:-$S3_CHUNKS_BUCKET}"
VDB_ENDPOINT="${VDB_ENDPOINT:-$S3_ENDPOINT}"
WEAVE_PROJECT="${WEAVE_PROJECT:-$WANDB_PROJECT}"
export VLM_URL VLM_MODEL EMBED_URL EMBED_MODEL ASSET_BUCKET VDB_ENDPOINT WEAVE_PROJECT
LLM_URL="${LLM_URL:-https://api.inference.wandb.ai/v1}"
LLM_MODEL_SMALL="${LLM_MODEL_SMALL:-meta-llama/Llama-3.1-8B-Instruct}"
LLM_MODEL_LARGE="${LLM_MODEL_LARGE:-nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B}"
LLM_API_KEY="${LLM_API_KEY:-$WANDB_API_KEY}"
export LLM_URL LLM_MODEL_SMALL LLM_MODEL_LARGE LLM_API_KEY
```

`VDB_SCHEMA`/`VDB_COLLECTION` keep the team config values (VSS is read-only). Our new tables are in `VASTDB_BUCKET.sva`. The starter kit does not give the W&B URL/model ids for `LLM_*`; the defaults are W&B Inference and two models that its `/v1/models` lists and that answer in JSON mode from the VM. The app sends `LLM_API_KEY` (the W&B API key) to `LLM_URL`, never `GPU_BEARER_TOKEN`. `ASSET_BUCKET` defaults to the `S3_CHUNKS_BUCKET` of our team, and we write only under the `sva/` prefix.

## 3. Secret (step 3 of the skill)

Inside the pod the team ingress host does not resolve, so `VSS_URL` points to the in-cluster VSS backend service (`video-backend-service:8000`, the target of the team `/api` ingress).

The pod does not inherit the VM environment. The explicit mapping below agrees with `config.example`/spec §12.2. The Secret output goes directly to `apply`; it is not written to disk and not echoed.

```bash
kubectl -n "$NS" create secret generic "${APP_NAME}-vss-creds" \
  --from-literal=VSS_URL="http://video-backend-service.${NS}.svc.cluster.local:8000" \
  --from-literal=VSS_USERNAME="$USERNAME" \
  --from-literal=VSS_PASSWORD="$PASSWORD" \
  --from-literal=S3_ENDPOINT="$S3_ENDPOINT" \
  --from-literal=ACCESS_KEY="$ACCESS_KEY" \
  --from-literal=SECRET_KEY="$SECRET_KEY" \
  --from-literal=ASSET_BUCKET="$ASSET_BUCKET" \
  --from-literal=VASTDB_BUCKET="$VASTDB_BUCKET" \
  --from-literal=VDB_SCHEMA="$VDB_SCHEMA" \
  --from-literal=VDB_COLLECTION="$VDB_COLLECTION" \
  --from-literal=VDB_ENDPOINT="$VDB_ENDPOINT" \
  --from-literal=VLM_URL="$VLM_URL" \
  --from-literal=VLM_MODEL="$VLM_MODEL" \
  --from-literal=EMBED_URL="$EMBED_URL" \
  --from-literal=EMBED_MODEL="$EMBED_MODEL" \
  --from-literal=YOLO_URL="$YOLO_URL" \
  --from-literal=GPU_BEARER_TOKEN="${GPU_BEARER_TOKEN:-}" \
  --from-literal=LLM_URL="$LLM_URL" \
  --from-literal=LLM_MODEL_SMALL="$LLM_MODEL_SMALL" \
  --from-literal=LLM_MODEL_LARGE="$LLM_MODEL_LARGE" \
  --from-literal=LLM_API_KEY="$LLM_API_KEY" \
  --from-literal=WANDB_API_KEY="$WANDB_API_KEY" \
  --from-literal=WANDB_TEAM="$WANDB_TEAM" \
  --from-literal=WANDB_PROJECT="$WANDB_PROJECT" \
  --from-literal=WEAVE_PROJECT="$WEAVE_PROJECT" \
  --dry-run=client -o yaml | kubectl apply -f -
```

## 4. ConfigMap (step 2 of the skill)

The bootstrap code ConfigMap contains only `bootstrap.py` and `requirements.txt`. It holds no libraries, models or videos. Non-secret runtime variables go in a separate environment ConfigMap.

```bash
bash app/scripts/configmap_size.sh
kubectl -n "$NS" create configmap "${APP_NAME}-code" \
  --from-file=bootstrap.py=app/scripts/bootstrap.py \
  --from-file=requirements.txt=app/requirements.txt \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NS" create configmap "${APP_NAME}-env" \
  --from-literal=SVA_REPO="$SVA_REPO" \
  --from-literal=SVA_COMMIT="$SVA_COMMIT" \
  --from-literal=SVA_TARBALL_SHA256="$SVA_TARBALL_SHA256" \
  --from-literal=MODE=site --from-literal=DATA_BACKEND=vastdb \
  --from-literal=ALLOW_DUCKDB_FALLBACK=0 --from-literal=PUBLIC_BASE_PATH=/app \
  --from-literal=PORT="$APP_PORT" --from-literal=ASSET_DIR=/state/assets \
  --from-literal=ASSET_SYNC=1 --from-literal=S3_VERIFY="${S3_VERIFY:-1}" \
  --from-literal=VDB_VERIFY="${VDB_VERIFY:-${S3_VERIFY:-1}}" \
  --from-literal=INDEX_DUCKDB=/state/index.duckdb \
  --from-literal=APP_DUCKDB=/state/app.duckdb \
  --from-literal=RUNS_DIR=/state/runs --from-literal=LIB_DIR=/state/lib \
  --dry-run=client -o yaml | kubectl apply -f -
```

The app serves the scene assets from `ASSET_DIR` instead of redirecting browsers to S3 presigned URLs: the team S3 endpoint has a self-signed certificate and its hostname resolves only inside the cluster/VM, so the browsers of the judges cannot load those URLs. With `ASSET_SYNC=1`, `bootstrap.py` mirrors `s3://$ASSET_BUCKET/sva/<scene_key>/…` and `sva/_map/…` into `/state/assets` (`pipeline/common/upload.py --download`) before `exec uvicorn` and logs one `download:` line with the counts and MB. TLS verification defaults to enabled. Install the site issuer’s trusted CA bundle and set `S3_VERIFY=/tls/ca-bundle.pem` (and `VDB_VERIFY` as needed); the optional `sva-trusted-ca` ConfigMap below must contain `ca-bundle.pem`. Explicit `0` is a diagnostic override, never the deployment default. Leaving `ASSET_DIR` empty uses stable `/api/assets/…` identities that redirect to a newly signed URL on each read.

## 5. Deployment, Service, Ingress (step 4 of the skill)

Use the public `python:3.12-slim`. Application state and run blobs share a dedicated **ReadWriteOnce persistent volume**, with one replica and `Recreate` updates so two DuckDB writer processes do not overlap. Choose `SVA_STATE_STORAGE_CLASS` for a filesystem with verified fsync and POSIX locking; do not assume arbitrary RWX/NFS is safe for DuckDB. Assets remain a separate RWX mirror. Install/mirror work and readiness probes are unchanged; a persistent state volume does not replace backups.

Before migrating an existing emptyDir deployment, stop the writer, use `state_backup.py backup` to export the current DB and runs to durable storage, provision the new volume, restore to clean paths, and only then start the new pod. Do not roll the old pod away before exporting its state. Keep the old snapshot until recovery verification succeeds. No site rollout is performed by this document update.

```bash
: "${SVA_STATE_STORAGE_CLASS:?Choose a tested single-writer state storage class}"
kubectl -n "$NS" apply -f - <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${APP_NAME}-assets
  labels: {app: ${APP_NAME}}
spec:
  accessModes: [ReadWriteMany]
  storageClassName: vastdata-filesystem
  resources: {requests: {storage: 30Gi}}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${APP_NAME}-state
  labels: {app: ${APP_NAME}}
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: ${SVA_STATE_STORAGE_CLASS}
  resources: {requests: {storage: 20Gi}}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${APP_NAME}
  labels: {app: ${APP_NAME}}
spec:
  replicas: 1
  strategy: {type: Recreate}
  selector:
    matchLabels: {app: ${APP_NAME}}
  template:
    metadata:
      labels: {app: ${APP_NAME}}
    spec:
      # the PVC (NFS) fails to mount on cosmos-cl3 ("rpc.statd is not running"); it mounts on cosmos-r18-124
      nodeSelector: {kubernetes.io/hostname: cosmos-r18-124}
      containers:
      - name: app
        image: python:3.12-slim
        imagePullPolicy: IfNotPresent
        ports:
        - containerPort: ${APP_PORT}
        envFrom:
        - secretRef: {name: ${APP_NAME}-vss-creds}
        - configMapRef: {name: ${APP_NAME}-env}
        volumeMounts:
        - {name: code, mountPath: /bootstrap, readOnly: true}
        - {name: state, mountPath: /state}
        - {name: assets, mountPath: /state/assets}
        - {name: trusted-ca, mountPath: /tls, readOnly: true}
        workingDir: /state
        command: ["python", "/bootstrap/bootstrap.py"]
        startupProbe:
          httpGet: {path: /health, port: ${APP_PORT}}
          periodSeconds: 10
          failureThreshold: 90
        readinessProbe:
          httpGet: {path: /health, port: ${APP_PORT}}
          initialDelaySeconds: 60
          periodSeconds: 10
      volumes:
      - name: code
        configMap: {name: ${APP_NAME}-code}
      - name: state
        persistentVolumeClaim: {claimName: ${APP_NAME}-state}
      - name: trusted-ca
        configMap: {name: sva-trusted-ca, optional: true}
      - name: assets
        persistentVolumeClaim: {claimName: ${APP_NAME}-assets}
---
apiVersion: v1
kind: Service
metadata:
  name: ${APP_NAME}
  labels: {app: ${APP_NAME}}
spec:
  selector: {app: ${APP_NAME}}
  ports:
  - {name: http, port: 80, targetPort: ${APP_PORT}}
  type: ClusterIP
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: ${APP_NAME}
  labels: {app: ${APP_NAME}}
  annotations:
    nginx.ingress.kubernetes.io/use-regex: "true"
    nginx.ingress.kubernetes.io/rewrite-target: /\$2
    nginx.ingress.kubernetes.io/proxy-buffering: "off"
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
spec:
  ingressClassName: nginx
  rules:
  - host: ${APP_HOST}
    http:
      paths:
      - path: /app(/|$)(.*)
        pathType: ImplementationSpecific
        backend:
          service:
            name: ${APP_NAME}
            port: {number: 80}
EOF
```

## 6. Shard fallback when GitHub is not reachable

Run in the same accepted source directory on the VM. The output is a multi-document YAML (each document uses the YAML-compatible JSON form), and each document is smaller than 900 KiB. `app/static/lib`, node_modules, caches and hidden files are not packed. The compressed source goes into `binaryData` under keys such as `source.part-0000`. A projected volume mounts the ConfigMaps at `/split`, and bootstrap joins them in order and does not download the GitHub tarball. Egress to pip and the browser library CDN is still necessary. The shards contain `app/`, `schemas/`, `pipeline/__init__.py` and `pipeline/common/*.py` (site mode imports the SQL schemas and `pipeline.common.site_map`); they hold no data.

```bash
bash app/scripts/configmap_size.sh --split > /tmp/sva-split.yaml
# server-side: client-side apply copies each ~900 KiB shard into an annotation (limit 256 KiB)
kubectl -n "$NS" apply --server-side --force-conflicts -f /tmp/sva-split.yaml
python3 - <<'PY' > /tmp/sva-split-patch.json
import json
from pathlib import Path
docs = [json.loads(part) for part in Path('/tmp/sva-split.yaml').read_text().split('---') if part.strip()]
sources = [{'configMap': {'name': d['metadata']['name']}} for d in docs]
print(json.dumps([
    {'op': 'add', 'path': '/spec/template/spec/volumes/-',
     'value': {'name': 'split', 'projected': {'sources': sources}}},
    {'op': 'add', 'path': '/spec/template/spec/containers/0/volumeMounts/-',
     'value': {'name': 'split', 'mountPath': '/split', 'readOnly': True}}
]))
PY
kubectl -n "$NS" patch deployment "$APP_NAME" --type=json --patch-file=/tmp/sva-split-patch.json
kubectl -n "$NS" set env deployment/"$APP_NAME" SVA_SPLIT_DIR=/split
kubectl -n "$NS" rollout restart deployment/"$APP_NAME"
```

The patch is for the first switch of the Deployment above to the fallback. When the source changes, rebuild the related shards and then restart. To go back to tarball mode, apply the Deployment of section 5 again (this restores the original volumes/envFrom) and run `kubectl -n "$NS" set env deployment/"$APP_NAME" SVA_SPLIT_DIR-`.

## Live deployment (2026-10-09)

The deployment at `/app` was made with a VM script that follows sections 3–6 with these differences:

- Split ConfigMap mode from the start: `sva-code-NNNN` from `configmap_size.sh --split` (server-side apply), mounted at `/split`, `SVA_SPLIT_DIR=/split`. The environment ConfigMap has no `SVA_REPO`, `SVA_COMMIT` or `SVA_TARBALL_SHA256`.
- The source is a frozen copy of one commit (`git archive`), vendored on the VM before the split. The Deployment template has the annotations `sva/source` (the commit) and `sva/split` (the first 16 hex digits of the SHA-256 of the split YAML), so a new commit always rolls the pod.
- The site map is a separate ConfigMap `sva-sitemap` with `site_map.json.gz` (output of `pipeline/common/site_map.py`), mounted read-only at `/sitemap`, with `SITE_MAP=/sitemap/site_map.json.gz`.
- The environment ConfigMap also sets `OMP_NUM_THREADS=4`.
- The Secret takes the GPU URLs and model ids from section 2 as fixed values (`VLM_URL` = Cosmos3 `:8001`, `EMBED_URL` = Cosmos-Embed1 `:8003`, `YOLO_URL` = `:8002`).

Check: `kubectl -n "$NS" get deploy "$APP_NAME" -o jsonpath='{.spec.template.metadata.annotations}'` shows the deployed commit.

## 7. curl and SSE checks (step 5 of the skill)

```bash
kubectl -n "$NS" rollout status deploy/"$APP_NAME"
kubectl -n "$NS" get pods,svc,ingress -l app="$APP_NAME"
kubectl -n "$NS" logs -l app="$APP_NAME" --tail=100
curl -sS -o /dev/null -w '%{http_code}\n' "http://${APP_HOST}/app"
curl -fSs "http://${APP_HOST}/app/health"
```

The page must return HTTP 200. Check `ok`, `mode=site`, `libs_ready`, `index_rows` and `assets_ok` in health. Open the app from the App button of the workshop; the internal address of the team host may not be open to the browsers of the judges.

Send one short question on a published scene. With `curl -N`, events such as `step`/`tool_call` must appear frame by frame and end with `status`/`done`; the output must not wait until the full answer is complete:

```bash
read -r -p 'Published warehouse scene_key: ' SCENE_KEY
export SCENE_KEY
python3 - <<'PY' > /tmp/sva-ask.json
import json, os
print(json.dumps({'q': 'When did the worker start to dodge the forklift?',
                  'context': {'workspace': 'warehouse', 'scene_key': os.environ['SCENE_KEY']},
                  'budget': {'tools': 8, 'seconds': 25}}, ensure_ascii=False))
PY
curl -fSsN -H 'Content-Type: application/json' --data-binary @/tmp/sva-ask.json \
  "http://${APP_HOST}/app/api/agent/ask"
```

Code update: run sections 2 and 4 again with the new commit, then `kubectl -n "$NS" rollout restart deploy/"$APP_NAME"`. Credential update: create the Secret again, then restart. Port/probe/Ingress update: apply section 5 again. For a 404 on `/app`, check the host, the rewrite and the path. For `ImagePullBackOff`, ask the organizers to confirm that Docker Hub is reachable. Do not change to another host or to local delivery.

## 8. Artifacts to the site (spec §12.4)

The ws lead packs the artifacts that passed the schema check into `sva-out-<analysis_rev>.tar.zst`, with each volume ≤ 2 GB, and puts them in a read-only HTTPS directory that the VM can reach (Tailscale Funnel; alternative: GitHub Release). Download and extract a full package on the VM. For a split package, first download each volume with `curl -fL` as the publisher list says, verify it, join the volumes in order with `cat`, and then extract.

The VM upload/VastDB check uses a separate venv (not the GPU pipeline environment):

```bash
python3 -m venv /tmp/sva-site-venv
/tmp/sva-site-venv/bin/python -m pip install -r app/requirements.txt
/tmp/sva-site-venv/bin/python - <<'PY'
import os
import urllib.request
import boto3
from botocore.config import Config
import pyarrow as pa
import vastdb

s3 = boto3.client('s3', endpoint_url=os.environ['S3_ENDPOINT'],
                  aws_access_key_id=os.environ['ACCESS_KEY'], aws_secret_access_key=os.environ['SECRET_KEY'],
                  config=Config(signature_version='s3v4'))
bucket, key = os.environ['ASSET_BUCKET'], 'sva/_deploy/probe.bin'
raw = b'x' * 1024
s3.put_object(Bucket=bucket, Key=key, Body=raw)
assert s3.get_object(Bucket=bucket, Key=key)['Body'].read() == raw
url = s3.generate_presigned_url('get_object', Params={'Bucket': bucket, 'Key': key}, ExpiresIn=300)
with urllib.request.urlopen(url) as response:
    assert response.read() == raw
with open('/tmp/sva-presigned-url', 'w', opener=lambda p, f: os.open(p, f, 0o600)) as dest:
    dest.write(url)
session = vastdb.connect(endpoint=os.environ['VDB_ENDPOINT'], access=os.environ['ACCESS_KEY'],
                         secret=os.environ['SECRET_KEY'],
                         ssl_verify=__import__('app.settings', fromlist=['Settings']).Settings().vdb_verify)
with session.transaction() as tx:
    schema = tx.bucket(os.environ['VASTDB_BUCKET']).create_schema('sva', fail_if_exists=False)
    table = schema.create_table('deploy_probe', pa.schema([('id', pa.int64())]), fail_if_exists=False)
    table.insert(pa.table({'id': [1, 2]}))
    rows = table.select(columns=['id']).read_all().num_rows
    assert rows >= 2
print('S3: 1024 bytes read back; VastDB sva.deploy_probe: two rows written and read back')
PY
```

`deploy_probe` is a diagnostic table in the new schema. Each run adds two more rows. Do not touch the official `vss-collection`/`vss-prompts-events`. The short-lived presigned URL is kept only in a file with mode 0600 on the VM; do not paste it into logs or commits. In the browser developer tools on site, read the URL from that file and check the 1024 bytes with `fetch(url).then(r => r.arrayBuffer())`. If the cross-origin read fails, CORS has failed. A successful HTTP read does not replace the browser test.

```bash
read -r -p 'HTTPS URL of the verified artifact package: ' ARTIFACT_URL
mkdir -p /tmp/sva-out
curl -fL "$ARTIFACT_URL" | tar --zstd -xf - -C /tmp/sva-out
/tmp/sva-site-venv/bin/python pipeline/common/schema_check.py /tmp/sva-out
test -f pipeline/common/upload.py && test -f pipeline/common/index_build.py || {
  echo 'Waiting for the upload and index build CLIs of P-C'; exit 1;
}
/tmp/sva-site-venv/bin/python pipeline/common/upload.py --help
/tmp/sva-site-venv/bin/python pipeline/common/index_build.py --help
```

These two CLIs are not merged yet, so do not invent input arguments. After they are merged, run `upload.py` on `/tmp/sva-out` as their help says, write to `VASTDB_BUCKET.sva` with `index_build.py --vastdb`, and read back the row count of each table. Binaries go to `s3://$ASSET_BUCKET/sva/<scene_key>/…`. At 09:30, first do the 1 KB object write/presigned read/browser CORS, then write two rows to the new schema and read them back. If the upload is slow, first upload the 12 demo runs + 2 trips. The normal demo does not turn on the DuckDB fallback. If the fallback is really necessary, set `ALLOW_DUCKDB_FALLBACK=1` explicitly, download `index.duckdb` to `/state/index.duckdb`, and make sure that the footer shows the degraded mode.

VSS login and re-ingest baseline (step 2 of the switch checklist): the JWT stays only in the Python process; it is not written to a file or to the output.

```bash
python3 - <<'PY'
import json, os
from pathlib import Path
import urllib.request

base = os.environ['INGRESS_URL'].rstrip('/') + '/api/v1'
payload = json.dumps({'username': os.environ['USERNAME'], 'password': os.environ['PASSWORD']}).encode()
request = urllib.request.Request(base + '/auth/login', data=payload,
                                 headers={'Content-Type': 'application/json'})
with urllib.request.urlopen(request) as response:
    token = json.load(response)['access_token']
for name, path in [('ingest-config', '/metadata/ingest-config'),
                   ('stats-before', '/dashboard/stats'),
                   ('explore-0000', '/videos/explore?scope=all&limit=100&offset=0')]:
    request = urllib.request.Request(base + path, headers={'Authorization': 'Bearer ' + token})
    with urllib.request.urlopen(request) as response:
        data = json.load(response)
    Path('/tmp/sva-' + name + '.json').write_text(json.dumps(data))
    if name == 'stats-before':
        print('re_ingest_rows baseline:', data['overview']['re_ingest_rows'])
print('VSS login OK; ingest config, baseline, and first Explore page saved')
PY
```

`explore-0000` is only the first page. P-C continues to page up to the `total` of the response and builds `site_map`; do not use the first page as the full mapping. The re-ingest of seat 2 follows the ingest skill of the starter kit; the baseline files are for its before/after comparison.

## 09:30 seven-step checklist (spec §12.5)

1. Seat 1: look at the environment variable names, probe the health and model ids of the four endpoints as in section 2, and record whether a token exists, but not its value.
2. Log in to the VSS of our team to get a JWT; save `/metadata/ingest-config`; record the `re_ingest_rows` baseline from `/dashboard/stats`; page `/videos/explore` up to `total`, and P-C `site_map.py` builds the mapping and shows the mapping rate. The calls follow the retrieval skills of the starter kit. Do not print the JWT.
3. Write 1 KB to the S3 of our team/presigned read/browser CORS; write two rows to the new VastDB `sva` schema and read them back. On failure, turn on the DuckDB fallback explicitly and show it in the UI. Do not change the two official VSS tables.
4. Section 8: get the artifact package → P-C upload → `index_build.py --vastdb` → read back and reconcile row counts.
5. Sections 3–7: Secret → bootstrap ConfigMap → Deployment/Service/Ingress → `/app/health` → workshop App button → short SSE check.
6. With `MODE=site`, run two preset questions to fill the cache, and keep the main question uncached. Confirm the Weave root op, the `vlm.*` host of the live `:8001` and its duration, and that the footer row counts agree with the Evaluation numbers.
7. Seat 2, in parallel with steps 3–6: re-ingest 4–8 chunks with the `ingest/reingest-videos` skill of the starter kit, with `custom_prompt` ≤ 800 characters. Poll the job every 4 seconds until `completed`. Record `prompt_rev` and the `re_ingest_rows` difference. P-C runs `retrieval_recall.py --before-after`. Do not ingest internet videos.

Delivery URL: `http://${APP_HOST}/app`. The host comes from the `INGRESS_URL` of our team. The judges enter from the workshop App button. Site credentials, egress, S3 CORS, VastDB read-back, full app health and SSE all need verification on site.


## 9. Application-state recovery and replication

Stop the application writer first. These commands never send notifications and
refuse to overwrite an existing restore destination:

```bash
python app/scripts/state_backup.py backup /backup/sva-snapshot --app /state/app.duckdb --runs /state/runs
python app/scripts/state_backup.py restore /backup/sva-snapshot --app /fresh/app.duckdb --runs /fresh/runs
```

The manifest verifies every database/blob/cache/outbox file before restore writes.
The whole database includes actions, **per-channel action_deliveries**, pending
mirror/blob outboxes, running jobs, sessions, turns, events and observations.
Blob outbox paths are relocated; restored historical trace references resolve
against the restored run directory. Verify row counts and run/blob hydration in
the clean instance before switching its state volume. Pin the same immutable
asset/index release separately. Backups may contain sensitive investigation data;
store them with the same access controls as application state.

Large trace outputs commit immutable local bytes first. `retry_blobs` copies them
outside the database lock with bounded exponential retry; mirror retries likewise
read current local rows. Pending copies survive restart and must be monitored.
Remote mirroring is replication, not an atomic distributed backup; a local-only
pending blob requires the backup's run files. Per-channel delivery records prevent
resending known successful channels after recovery. A crash after an external
provider accepts a message but before the local receipt remains ambiguous; retain
provider idempotency keys and do not promise external exactly-once delivery.
