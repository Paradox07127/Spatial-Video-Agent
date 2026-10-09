"""Fill SDG run directories with the per-camera files listed in meta.json, read by HTTP Range from the HF artifact shards.

  python pipeline/warehouse/fetch_sdg.py --runs all|N --cams all|ceiling_00,... --artifacts rgb,depth,calib,gt

Runs come from selected_runs.json (N = the first N). Member offsets come from the local tar index (index/*.json). A file is
downloaded to <name>.part (resumed from its size), size-checked, then renamed; existing files are never touched.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common import lease  # noqa: E402
from pipeline.warehouse.sdg import SDG_ROOT  # noqa: E402

HF_BASE = "https://huggingface.co/datasets/nvidia/PhysicalAI-WorldModel-Synthetic-Warehouse-Operations-Scenes/resolve/main/"
ARTIFACTS = {"rgb": "rgb", "depth": "depth", "calib": "camera_params", "gt": "object_detection"}
CHUNK = 1 << 20
WORKERS = 4


def member_index(root: Path) -> dict[str, tuple[str, int, int]]:
    """tar member name -> (shard path in repo, byte offset, size)."""
    out = {}
    for f in sorted((root / "index").glob("*.json")):
        d = json.loads(f.read_text())
        for m in d["members"]:
            out[m["name"]] = (d["path"], m["offset"], m["size"])
    return out


def plan(run: str, cams: list[str] | None, kinds: list[str], index: dict) -> list[tuple[Path, str, int, int]]:
    """(destination, shard, offset, size) for every requested file of the run that is not on disk yet."""
    run_dir = SDG_ROOT / run
    meta = json.loads((run_dir / "meta.json").read_text())
    todo = []
    for c in meta["cameras"]:
        if cams and c["camera_alias"] not in cams:
            continue
        for k in kinds:
            name = c["members"][ARTIFACTS[k]]
            dst = run_dir / name[len(meta["run_id"]) + 1:]  # members are "<hash>_<run>.<cam>.<kind>"; local files drop the prefix
            if not dst.exists():
                todo.append((dst, *index[name]))
    return todo


def fetch(dst: Path, shard: str, offset: int, size: int) -> int:
    part = dst.with_name(dst.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    if have < size:
        req = urllib.request.Request(HF_BASE + shard, headers={"Range": f"bytes={offset + have}-{offset + size - 1}"})
        with urllib.request.urlopen(req, timeout=60) as r, open(part, "ab") as f:
            if r.status != 206:
                raise RuntimeError(f"{dst.name}: HTTP {r.status}, expected 206")
            while b := r.read(CHUNK):
                f.write(b)
    if part.stat().st_size != size:
        raise RuntimeError(f"{dst.name}: {part.stat().st_size} B, expected {size} B (rerun to resume)")
    part.rename(dst)
    return size


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default="all", help="all | N (first N of selected_runs.json)")
    ap.add_argument("--cams", default="all")
    ap.add_argument("--artifacts", default="rgb,depth,calib,gt")
    a = ap.parse_args()
    runs = [r["run_id"] for r in json.loads((SDG_ROOT.parent / "selected_runs.json").read_text())]
    if a.runs != "all":
        runs = runs[:int(a.runs)]
    cams = None if a.cams == "all" else a.cams.split(",")
    kinds = a.artifacts.split(",")
    index = member_index(SDG_ROOT.parent)
    with lease.acquire(cpu=WORKERS, owner="P-W1"):
        for run in runs:
            todo = plan(run, cams, kinds, index)
            with ThreadPoolExecutor(WORKERS) as ex:
                total = sum(ex.map(lambda t: fetch(*t), todo))
            print(f"{run}: fetched {len(todo)} files, {total / 1e6:.1f} MB", flush=True)


if __name__ == "__main__":
    main()
