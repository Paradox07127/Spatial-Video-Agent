"""Content-aware resumable uploads and atomic asset downloads."""
import hashlib
import sys
from pathlib import Path

import pytest

from pipeline.common import upload


class S3:
    def __init__(self, content, metadata=None, etag=None):
        self.content = content
        self.metadata = metadata or {}
        self.etag = etag or f'"{hashlib.md5(content, usedforsecurity=False).hexdigest()}"'
        self.sent = []

    def get_paginator(self, name):
        return self

    def paginate(self, **kw):
        return [{'Contents': [{'Key': 'sva/wh-fixture/data.json', 'Size': len(self.content), 'ETag': self.etag}]}]

    def head_object(self, **kw):
        return {'Metadata': self.metadata}

    def upload_file(self, filename, bucket, key, ExtraArgs):
        self.sent.append((key, Path(filename).read_bytes(), ExtraArgs))


@pytest.mark.parametrize('old,new,expected', [(b'[1]', b'[2]', 1), (b'[1]', b'[1]', 0)])
def test_upload_compares_content_when_sizes_match(tmp_path, monkeypatch, old, new, expected):
    scene = tmp_path / 'wh-fixture'
    scene.mkdir()
    (scene / 'data.json').write_bytes(new)
    s3 = S3(old)
    monkeypatch.setattr(upload, 'scene_dirs', lambda _: [scene])
    monkeypatch.setattr(upload, 's3_client', lambda: s3)
    monkeypatch.setenv('ASSET_BUCKET', 'bucket')
    monkeypatch.setattr(sys, 'argv', ['upload.py', str(tmp_path)])
    upload.main()
    assert len(s3.sent) == expected
    if expected:
        assert s3.sent[0][2]['Metadata']['sha256'] == hashlib.sha256(new).hexdigest()


def test_multipart_upload_uses_checksum_metadata(tmp_path, monkeypatch):
    scene = tmp_path / 'wh-fixture'
    scene.mkdir()
    content = b'[1]'
    (scene / 'data.json').write_bytes(content)
    s3 = S3(content, {'sha256': hashlib.sha256(content).hexdigest()}, '"multipart-2"')
    monkeypatch.setattr(upload, 'scene_dirs', lambda _: [scene])
    monkeypatch.setattr(upload, 's3_client', lambda: s3)
    monkeypatch.setenv('ASSET_BUCKET', 'bucket')
    monkeypatch.setattr(sys, 'argv', ['upload.py', str(tmp_path)])
    upload.main()
    assert not s3.sent


def test_failed_download_does_not_replace_published_asset(tmp_path):
    s3 = S3(b'[2]')
    scene = tmp_path / 'wh-fixture'
    scene.mkdir()
    target = scene / 'data.json'
    target.write_bytes(b'[old]')

    def fail(bucket, key, filename):
        Path(filename).write_bytes(b'[')
        raise OSError('Interrupted download')

    s3.download_file = fail
    with pytest.raises(OSError):
        upload.download(s3, 'bucket', tmp_path, [])
    assert target.read_bytes() == b'[old]'
    assert list(scene.iterdir()) == [target]


def test_download_refetches_equal_size_content_despite_newer_local_mtime(tmp_path):
    from pipeline.common.test_upload import FakeS3
    import os
    from datetime import datetime, timezone
    scene = tmp_path / 'wh-fixture'
    scene.mkdir()
    target = scene / 'data.json'
    target.write_bytes(b'[1]')
    os.utime(target, (1000.8, 1000.8))
    s3 = FakeS3({'sva/wh-fixture/data.json': b'[2]'},
                {'sva/wh-fixture/data.json': datetime.fromtimestamp(1000.0, timezone.utc)})
    assert upload.download(s3, 'bucket', tmp_path, []) == (1, 0, 3)
    assert target.read_bytes() == b'[2]'


def test_legacy_multipart_checksum_skips_unchanged_large_asset(tmp_path):
    content = b'a' * (8 << 20) + b'b' * 31
    target = tmp_path / 'large.bin'
    target.write_bytes(content)
    parts = [content[:8 << 20], content[8 << 20:]]
    digest = hashlib.md5(b''.join(hashlib.md5(p, usedforsecurity=False).digest() for p in parts), usedforsecurity=False).hexdigest()
    s3 = S3(content, etag=f'"{digest}-2"')
    s3.head_object = lambda **kw: pytest.fail('matching multipart ETag needs no HEAD')
    assert upload.same_content(s3, 'bucket', {'Key': 'sva/wh-fixture/large.bin', 'ETag': s3.etag}, upload.checksums(target))
