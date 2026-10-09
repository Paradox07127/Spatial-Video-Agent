#!/usr/bin/env bash
# Integration gate. Run from anywhere:  bash sva/app/scripts/gate.sh [--w0]
#   --w0  contract wave: contract tests, fixture schema check, fixture determinism, vendor manifest, playwright config, secrets
#   (none) full gate: all pytest, fixture schema check, determinism, playwright e2e (both projects), configmap size, secrets
set -uo pipefail
SVA="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$SVA"
PY="$SVA/.venv/bin/python"
MODE="${1:-full}"
FAIL=0
step() { echo; echo "== $1"; }
check() { if [ "$1" -eq 0 ]; then echo "-- ok: $2"; else echo "-- FAIL: $2"; FAIL=1; fi; }

[ -x "$PY" ] || { echo "missing $PY (uv venv -p 3.12 .venv && uv pip install -r app/requirements.txt)"; exit 2; }

step "pytest"
if [ "$MODE" = "--w0" ]; then "$PY" -m pytest app/tests/contract -q; else "$PY" -m pytest app/tests -q; fi
check $? "pytest"

step "schema_check fixtures/"
"$PY" pipeline/common/schema_check.py fixtures/
check $? "schema_check"

step "fixture determinism (regenerate JSON + points.bin, compare with committed)"
TMP="$(mktemp -d)"
"$PY" fixtures/gen_fixture.py --no-media --out "$TMP" >/dev/null && "$PY" - "$TMP" <<'PYEOF'
import sys
from pathlib import Path
new, old = Path(sys.argv[1]), Path("fixtures")
gen = {p.relative_to(new) for p in new.rglob("*") if p.is_file()}
HAND_MADE = {"labels/fleet_rules.json", "labels/warehouse_rules.json", "sva-out/_map/style.json"}  # committed by hand, not written by gen_fixture.py
committed = {p.relative_to(old) for p in old.rglob("*") if p.is_file() and p.suffix in (".json", ".geojson", ".bin") and p.relative_to(old).parts[0] != "vss" and str(p.relative_to(old)) not in HAND_MADE}
diff = sorted(str(p) for p in gen if (old / p).read_bytes() != (new / p).read_bytes() if (old / p).exists())
missing = sorted(str(p) for p in gen - committed) + sorted(str(p) for p in committed - gen)
print(f"{len(gen)} generated files; differing: {diff or 'none'}; unmatched: {missing or 'none'}")
sys.exit(1 if diff or missing else 0)
PYEOF
check $? "determinism"
rm -rf "$TMP"

step "vendor_manifest.json"
"$PY" - <<'PYEOF'
import json, re, sys
m = json.load(open("app/scripts/vendor_manifest.json"))
bad = [f["name"] for f in m["files"] if not (re.fullmatch(r"[0-9a-f]{64}", f["sha256"]) and f["url"].startswith("https://") and f["size"] > 0)]
print(f"{len(m['files'])} files, importmap {sorted(m['importmap'])}, bad: {bad or 'none'}")
sys.exit(1 if bad else 0)
PYEOF
check $? "vendor manifest"
if [ "$MODE" != "--w0" ] && [ -f app/scripts/vendor.py ]; then
  "$PY" app/scripts/vendor.py --check; check $? "vendor.py --check"
fi

step "playwright"
if [ "$MODE" = "--w0" ]; then
  (cd app && npx playwright test -c tests/playwright.config.js --list --pass-with-no-tests)
  check $? "playwright config loads"
else
  (cd app && npx playwright test -c tests/playwright.config.js --pass-with-no-tests)
  check $? "playwright e2e"
fi

if [ "$MODE" != "--w0" ]; then
  step "configmap size"
  if [ -f app/scripts/configmap_size.sh ]; then bash app/scripts/configmap_size.sh; check $? "configmap < 900 KiB"; else echo "-- SKIP: app/scripts/configmap_size.sh not merged yet (card D)"; fi
fi

step "secrets"
HITS="$(grep -rInE '(sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|(api[_-]?key|secret[_-]?key|password|token)["'"'"']?[[:space:]]*[:=][[:space:]]*["'"'"'][A-Za-z0-9/+_-]{16,})' \
  --exclude-dir=.venv --exclude-dir=node_modules --exclude-dir=.e2e --exclude-dir=lib --exclude='*.mp4' --exclude='*.jpg' --exclude='*.bin' . || true)"
if [ -n "$HITS" ]; then echo "$HITS" | cut -c1-160; check 1 "no secrets in sva/"; else check 0 "no secrets in sva/"; fi

echo
if [ "$FAIL" -eq 0 ]; then echo "gate: PASS ($MODE)"; else echo "gate: FAIL ($MODE)"; fi
exit "$FAIL"
