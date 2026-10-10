"""upload.py dry run and --download mirror.  .venv/bin/python -m pytest pipeline/common/test_upload.py -q"""
import subprocess
import hashlib
import sys
import types
from pathlib import Path

from pipeline.common import upload

SVA = Path(__file__).resolve().parents[2]


class FakeS3:
    def __init__(self, objects, modified=None):
        self.objects, self.fetched, self.modified = objects, [], modified or {}

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return self

    def paginate(self, Bucket, Prefix):
        assert (Bucket, Prefix) == ("bucket", "sva/")
        rows = [{"Key": k, "Size": len(v), 'ETag': f'"{hashlib.md5(v, usedforsecurity=False).hexdigest()}"',
                 **({"LastModified": self.modified[k]} if k in self.modified else {})}
                for k, v in sorted(self.objects.items())]
        return [{"Contents": rows[:2]}, {"Contents": rows[2:]}, {}]

    def download_file(self, bucket, key, filename):
        self.fetched.append(key)
        Path(filename).write_bytes(self.objects[key])

    def head_object(self, **kwargs):
        return {'Metadata': {}}


def test_download_fetches_only_missing_or_resized_objects(tmp_path):
    s3 = FakeS3({"sva/wh-1/a.bin": b"aaaa", "sva/wh-1/sub/b.json": b"{}", "sva/fl-2/c.bin": b"cc",
                 "sva/_map/style.json": b"{\"v\":1}"})
    (tmp_path / "wh-1/sub").mkdir(parents=True)
    (tmp_path / "wh-1/a.bin").write_bytes(b"aaaa")
    (tmp_path / "wh-1/sub/b.json").write_bytes(b"{ }")
    assert upload.download(s3, "bucket", tmp_path, ["wh-1"]) == (2, 1, 9)
    assert sorted(s3.fetched) == ["sva/_map/style.json", "sva/wh-1/sub/b.json"]
    assert (tmp_path / "wh-1/sub/b.json").read_bytes() == b"{}" and (tmp_path / "_map/style.json").exists()
    assert not (tmp_path / "fl-2").exists()
    s3.fetched.clear()
    assert upload.download(s3, "bucket", tmp_path, []) == (1, 3, 2)
    assert s3.fetched == ["sva/fl-2/c.bin"]


def test_download_refetches_same_size_objects_newer_than_the_local_file(tmp_path):
    import os
    from datetime import datetime, timezone
    t = datetime(2026, 10, 9, 20, 0, tzinfo=timezone.utc)
    s3 = FakeS3({"sva/wh-1/new.json": b"[2]", "sva/wh-1/old.json": b"[1]"}, {"sva/wh-1/new.json": t, "sva/wh-1/old.json": t})
    (tmp_path / "wh-1").mkdir()
    for name, mtime in [("new.json", t.timestamp() - 60), ("old.json", t.timestamp() + 60)]:
        (tmp_path / "wh-1" / name).write_bytes(b"[0]" if name == 'new.json' else b"[1]")
        os.utime(tmp_path / "wh-1" / name, (mtime, mtime))
    assert upload.download(s3, "bucket", tmp_path, ["wh-1"]) == (1, 1, 3)
    assert s3.fetched == ["sva/wh-1/new.json"] and (tmp_path / "wh-1/new.json").read_bytes() == b"[2]"


def test_download_skips_keys_outside_dest_and_agent_runs(tmp_path):
    dest = tmp_path / "assets"
    # an absolute key like sva//tmp/x, pointed inside tmp_path so a failing run writes nowhere else
    s3 = FakeS3({"sva/../x": b"x", f"sva/{tmp_path}/x": b"x", "sva/runs/r_1/step_0.json": b"{}", "sva/wh-1/../../y": b"y",
                 "sva/wh-1/a.bin": b"aaaa"})
    assert upload.download(s3, "bucket", dest, []) == (1, 0, 4)
    assert s3.fetched == ["sva/wh-1/a.bin"]
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()) == ["assets/wh-1/a.bin"]


def test_s3_verify_reaches_boto3(monkeypatch):
    clients = []
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda *a, **k: clients.append(k)))
    for key in ("S3_ENDPOINT", "ACCESS_KEY", "SECRET_KEY"):
        monkeypatch.setenv(key, "x")
    monkeypatch.setenv("S3_VERIFY", "0")
    upload.s3_client()
    monkeypatch.delenv("S3_VERIFY")
    upload.s3_client()
    assert [c["verify"] for c in clients] == [False, True]


def test_dry_run_sends_the_shared_map_assets():
    out = subprocess.run([sys.executable, str(SVA / "pipeline/common/upload.py"), "--dry-run", str(SVA / "fixtures/sva-out")],
                         capture_output=True, text=True, check=True).stdout
    assert all(f"sva/_map/{n} " in out for n in ("basemap.pmtiles", "buildings.pmtiles", "style.json"))
