"""Single publisher: moves a card's scene products from sva-out/_tmp/<card>/<scene_key>/ into sva-out/<scene_key>/ atomically.

  python publish.py CARD SCENE_KEY [--out-root DIR]

The staged scene = the published scene (if any) overlaid with the card's files. A card that adds files to a scene another
card owns (cosmos records, for example) ships `manifest.patch.json` = {"assets": {...}, "models": {...}} instead of a
manifest.json; the patch is merged into the published manifest. The staged scene must pass schema_check; then the old
directory is swapped out with renames, so an interrupted publish never leaves a half-written sva-out/<scene_key>/.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common import paths  # noqa: E402
from pipeline.common.schema_check import SceneCheck  # noqa: E402

OUT_ROOT = paths.OUT
PATCH = "manifest.patch.json"


class PublishError(RuntimeError):
    pass


def tmp_dir(card: str, scene_key: str, *, out_root: Path = OUT_ROOT) -> Path:
    """Where a card writes its products: <out_root>/_tmp/<card>/<scene_key>/."""
    d = out_root / "_tmp" / card / scene_key
    d.mkdir(parents=True, exist_ok=True)
    return d


def publish(card: str, scene_key: str, *, out_root: Path = OUT_ROOT, check: bool = True) -> Path:
    """Returns the published directory; raises PublishError when schema_check fails (nothing is moved)."""
    src = out_root / "_tmp" / card / scene_key
    dst = out_root / scene_key
    if not src.is_dir():
        raise PublishError(f"no products at {src}")
    staging = out_root / f".{scene_key}.staging"
    old = out_root / f".{scene_key}.old"
    for d in (staging, old):
        if d.exists():
            shutil.rmtree(d)
    if dst.is_dir():
        shutil.copytree(dst, staging)
    shutil.copytree(src, staging, dirs_exist_ok=True, ignore=shutil.ignore_patterns(PATCH))
    patch = src / PATCH
    if patch.is_file():
        mpath = staging / "manifest.json"
        if not mpath.is_file():
            shutil.rmtree(staging)
            raise PublishError(f"{card} ships {PATCH} but {scene_key} has no published manifest")
        m, p = json.loads(mpath.read_text()), json.loads(patch.read_text())
        m["assets"].update(p.get("assets", {}))
        m["pipeline"]["models"].update(p.get("models", {}))
        tmp = staging / "manifest.json.tmp"
        tmp.write_text(json.dumps(m, ensure_ascii=False))
        os.replace(tmp, mpath)
    if check:
        c = SceneCheck(staging).run()
        # SceneCheck compares the directory name with scene_key; the staging name differs by design
        errs = [e for e in c.errors if "directory name" not in e]
        if errs:
            shutil.rmtree(staging)
            raise PublishError(f"{scene_key}: schema_check failed ({len(errs)}): " + "; ".join(errs[:5]))
    if dst.exists():
        os.rename(dst, old)
    os.rename(staging, dst)
    if old.exists():
        shutil.rmtree(old)
    print(f"published {card} -> {dst}", file=sys.stderr)
    return dst


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("card")
    ap.add_argument("scene_key")
    ap.add_argument("--out-root", default=str(OUT_ROOT))
    a = ap.parse_args()
    try:
        print(publish(a.card, a.scene_key, out_root=Path(a.out_root)))
    except PublishError as e:
        sys.exit(f"FAIL {e}")
