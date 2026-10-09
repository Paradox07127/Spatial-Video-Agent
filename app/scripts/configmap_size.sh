#!/usr/bin/env bash
set -euo pipefail
SVA="$(cd "$(dirname "$0")/../.." && pwd)"
PY="$SVA/.venv/bin/python"
if [ ! -x "$PY" ]; then PY=python3; fi
"$PY" - "$SVA" "$@" <<'PY'
import base64
import io
import json
from pathlib import Path
import sys
import tarfile

root = Path(sys.argv[1])
limit = 900 * 1024
if len(sys.argv) == 2:
    total = sum(p.stat().st_size for p in (root / "app/scripts/bootstrap.py", root / "app/requirements.txt"))
    ok = total < limit
    print(f"bootstrap ConfigMap: {total} bytes; limit {limit} bytes (< 900 KiB); {'OK' if ok else 'TOO LARGE'}")
    sys.exit(0 if ok else 1)
if sys.argv[2:] != ["--split"]:
    sys.exit("usage: configmap_size.sh [--split]")
buffer = io.BytesIO()
with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
    # site mode imports schemas/*.sql and pipeline.common.site_map, so they ship with app/
    for path in sorted([*(root / "app").rglob("*"), *(root / "schemas").rglob("*"),
                        root / "pipeline/__init__.py", *(root / "pipeline/common").glob("*.py")]):
        rel = path.relative_to(root)
        if (not path.is_file() or path.name == "config.env"
                or any(p in {"node_modules", "__pycache__", ".pytest_cache", "test-results", "playwright-report"}
                                      or p.startswith(".") for p in rel.parts)
                or rel.parts[:3] == ("app", "static", "lib")):
            continue
        archive.add(path, arcname=str(rel), recursive=False)
raw = buffer.getvalue()
# Base64 and YAML metadata must also fit below the 900 KiB safety margin.
chunk_size = 660 * 1024
for i, start in enumerate(range(0, len(raw), chunk_size)):
    doc = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": f"sva-code-{i:04d}"},
           "binaryData": {f"source.part-{i:04d}": base64.b64encode(raw[start:start + chunk_size]).decode()}}
    text = json.dumps(doc, indent=2)
    if len(text.encode()) >= limit:
        sys.exit("split ConfigMap TOO LARGE")
    print("---\n" + text)
PY
