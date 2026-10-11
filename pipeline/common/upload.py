"""Upload scene directories and the shared _map/ directory to s3://<ASSET_BUCKET>/sva/<scene_key or _map>/<relpath>
(S3_ENDPOINT, ACCESS_KEY, SECRET_KEY).

  python pipeline/common/upload.py [--dry-run] SRC [SCENE_KEY ...]
  python pipeline/common/upload.py --download DEST [SCENE_KEY ...]

SRC holds scene directories (sva-out or an unpacked bundle). Resumable: an object is skipped only when its size and
content checksum match. --dry-run lists what would be sent.
--download mirrors the other way, s3://<ASSET_BUCKET>/sva/<scene_key>/… and sva/_map/… to DEST/<scene_key>/…, skipping
files whose size and checksum match, sva/runs/… and any key that would resolve outside DEST.
S3_VERIFY=0 turns off TLS verification (the internal endpoint is self-signed).
"""
from __future__ import annotations

import argparse
import hashlib
import mimetypes
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common.schema_check import scene_dirs  # noqa: E402

PREFIX = "sva"


def s3_client():
    import boto3
    return boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT"], aws_access_key_id=os.environ["ACCESS_KEY"],
                        aws_secret_access_key=os.environ["SECRET_KEY"], verify=__import__("app.settings",fromlist=["Settings"]).Settings().s3_verify)


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
            if f.is_file() and f.stat().st_size == o["Size"] and same_content(s3, bucket, o, checksums(f)):
                skipped += 1
                continue
            f.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=f.parent, prefix='.' + f.name + '.', delete=False) as tmp:
                pending = Path(tmp.name)
            try:
                s3.download_file(bucket, o["Key"], str(pending))
                if pending.stat().st_size != o['Size']:
                    raise OSError(f"Incomplete download: {o['Key']}")
                pending.replace(f)
            finally:
                pending.unlink(missing_ok=True)
            fetched += 1
            size += o["Size"]
    return fetched, skipped, size


def checksums(path: Path) -> tuple[str, str, str]:
    sha, md5, part = hashlib.sha256(), hashlib.md5(usedforsecurity=False), hashlib.md5(usedforsecurity=False)
    parts, part_size = [], 0
    with path.open('rb') as stream:
        while block := stream.read(1 << 20):
            sha.update(block)
            md5.update(block)
            part.update(block)
            part_size += len(block)
            if part_size == 8 << 20:
                parts.append(part.digest())
                part, part_size = hashlib.md5(usedforsecurity=False), 0
    if part_size:
        parts.append(part.digest())
    multipart = f"{hashlib.md5(b''.join(parts), usedforsecurity=False).hexdigest()}-{len(parts)}"
    return sha.hexdigest(), md5.hexdigest(), multipart


def same_content(s3, bucket: str, obj: dict, hashes: tuple[str, str, str]) -> bool:
    sha, md5, multipart = hashes
    # Legacy uploads use ordinary MD5 ETags or boto3's default 8 MiB multipart ETags.
    if obj.get('ETag', '').strip('"') in (md5, multipart):
        return True
    return s3.head_object(Bucket=bucket, Key=obj['Key']).get('Metadata', {}).get('sha256') == sha


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
        have |= {o["Key"]: o for o in page.get("Contents", [])}
    sent = skipped = 0
    for f, key in files:
        hashes = checksums(f)
        sha = hashes[0]
        old = have.get(key)
        if old and old['Size'] == f.stat().st_size:
            if same_content(s3, bucket, old, hashes):
                skipped += 1
                continue
        # the browser plays and parses these through presigned URLs, so the type must be right
        s3.upload_file(str(f), bucket, key, ExtraArgs={"ContentType": mimetypes.guess_type(f.name)[0] or "application/octet-stream",
                                                     "Metadata": {"sha256": sha}})
        sent += 1
    print(f"upload: {sent} sent, {skipped} already present, {len(files)} files -> s3://{bucket}/{PREFIX}/")


if __name__ == "__main__":
    main()
