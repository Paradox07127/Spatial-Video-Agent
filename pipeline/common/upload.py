"""Upload scene directories and the shared _map/ directory to s3://<ASSET_BUCKET>/sva/<scene_key or _map>/<relpath>
(S3_ENDPOINT, ACCESS_KEY, SECRET_KEY).

  python pipeline/common/upload.py [--dry-run] SRC [SCENE_KEY ...]
  python pipeline/common/upload.py --download DEST [SCENE_KEY ...]

SRC holds scene directories (sva-out or an unpacked bundle). Resumable: an object that already exists with the same size
is skipped, so a rerun after an interruption only sends what is missing. --dry-run lists what would be sent.
--download mirrors the other way, s3://<ASSET_BUCKET>/sva/<scene_key>/… and sva/_map/… to DEST/<scene_key>/…, skipping
files whose size already matches and that are not older than the object, sva/runs/… and any key that would resolve outside DEST. S3_VERIFY=0 turns off TLS verification (the internal endpoint is self-signed).
"""
from __future__ import annotations

import argparse
import mimetypes
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common.schema_check import scene_dirs  # noqa: E402

PREFIX = "sva"


def s3_client():
    import boto3
    return boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT"], aws_access_key_id=os.environ["ACCESS_KEY"],
                        aws_secret_access_key=os.environ["SECRET_KEY"], verify=os.environ.get("S3_VERIFY", "1") != "0")


def download(s3, bucket: str, dest: Path, scenes: list[str]) -> tuple[int, int, int]:
    """Mirror sva/<scene_key>/… (only `scenes` when given) and sva/_map/… to dest; (fetched, skipped, bytes fetched)."""
    fetched = skipped = size = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=PREFIX + "/"):
        for o in page.get("Contents", []):
            rel = o["Key"][len(PREFIX) + 1:]
            top = rel.split("/", 1)[0]
            f = dest / rel
            # sva/runs/ holds agent traces, which must not land in the directory /api/assets serves
            if ("/" not in rel or rel.endswith("/") or top == "runs" or scenes and top not in [*scenes, "_map"]
                    or not f.resolve().is_relative_to(dest.resolve())):
                continue
            # the mirror persists across deploys, so a same-size object rewritten after the local copy is fetched again
            if f.is_file() and f.stat().st_size == o["Size"] and (
                    "LastModified" not in o or f.stat().st_mtime >= o["LastModified"].timestamp()):
                skipped += 1
                continue
            f.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, o["Key"], str(f))
            fetched += 1
            size += o["Size"]
    return fetched, skipped, size


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src", help="SRC, or DEST with --download")
    ap.add_argument("scenes", nargs="*", help="scene keys (default: every scene under SRC)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--download", action="store_true", help="mirror the bucket into SRC instead of uploading it")
    a = ap.parse_args()
    if a.download:
        bucket = os.environ["ASSET_BUCKET"]
        fetched, skipped, size = download(s3_client(), bucket, Path(a.src), a.scenes)
        print(f"download: {fetched} fetched ({size / 1e6:.1f} MB), {skipped} already present, "
              f"s3://{bucket}/{PREFIX}/ -> {a.src}", flush=True)
        return
    dirs = scene_dirs(Path(a.src)) + [d for d in [Path(a.src) / "_map"] if d.is_dir()]
    files = [(f, f"{PREFIX}/{s.name}/{f.relative_to(s).as_posix()}")
             for s in dirs if not a.scenes or s.name in a.scenes
             for f in sorted(s.rglob("*")) if f.is_file()]
    if a.dry_run:
        for f, key in files:
            print(f"  {key} {f.stat().st_size}")
        print(f"upload dry run: {len(files)} files, {sum(f.stat().st_size for f, _ in files) / 1e6:.1f} MB -> "
              f"s3://{os.environ.get('ASSET_BUCKET', '<ASSET_BUCKET>')}/{PREFIX}/")
        return
    s3 = s3_client()
    bucket = os.environ["ASSET_BUCKET"]
    have = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=PREFIX + "/"):
        have |= {o["Key"]: o["Size"] for o in page.get("Contents", [])}
    sent = skipped = 0
    for f, key in files:
        if have.get(key) == f.stat().st_size:
            skipped += 1
            continue
        # the browser plays and parses these through presigned URLs, so the type must be right
        s3.upload_file(str(f), bucket, key, ExtraArgs={"ContentType": mimetypes.guess_type(f.name)[0] or "application/octet-stream"})
        sent += 1
    print(f"upload: {sent} sent, {skipped} already present, {len(files)} files -> s3://{bucket}/{PREFIX}/")


if __name__ == "__main__":
    main()
