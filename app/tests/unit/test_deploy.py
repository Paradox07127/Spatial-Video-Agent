import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import urllib.request

import pytest


SVA = Path(__file__).resolve().parents[3]
SCRIPTS = SVA / "app/scripts"
LIMIT = 900 * 1024


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def manifest(tmp_path, files):
    path = tmp_path / "vendor_manifest.json"
    path.write_text(json.dumps({"importmap": {"test": files[0]["name"]}, "files": files}))
    return path


def vendor_file(name="test.js", raw=b"export const value = 1;"):
    return {"name": name, "url": "https://example.org/test.js",
            "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}


def test_vendor_check_cli(tmp_path):
    result = subprocess.run([sys.executable, str(SCRIPTS / "vendor.py"), "--check"],
                            env={**os.environ, "LIB_DIR": str(tmp_path / "missing")},
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "19 files" in result.stdout and "0 local" in result.stdout
    assert not (tmp_path / "missing").exists()


def test_vendor_check_offline_and_local_hash(tmp_path, monkeypatch):
    vendor = load_script("vendor")
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "test.js").write_bytes(b"export const value = 1;")
    monkeypatch.setattr(vendor, "MANIFEST", manifest(tmp_path, [vendor_file()]))
    monkeypatch.setenv("LIB_DIR", str(lib))
    monkeypatch.setattr(sys, "argv", ["vendor.py", "--check"])
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("check went online"))
    vendor.main()
    (lib / "test.js").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="sha256"):
        vendor.main()


@pytest.mark.parametrize("change", [{"name": "../test.js"}, {"sha256": "invalid"},
                                     {"url": "http://example.org/test.js"}, {"size": 0}])
def test_vendor_rejects_bad_manifest(tmp_path, monkeypatch, change):
    vendor = load_script("vendor")
    monkeypatch.setattr(vendor, "MANIFEST", manifest(tmp_path, [{**vendor_file(), **change}]))
    with pytest.raises(ValueError):
        vendor.read_manifest()


@pytest.mark.parametrize("data", [[], {"files": [None]}, {"files": [{"name": None}]},
                                  {"files": [{**vendor_file(), "sha256": None}]},
                                  {"files": [{**vendor_file(), "url": None}]},
                                  {"files": [vendor_file(".")]},
                                  {"files": [vendor_file(), vendor_file()]}])
def test_vendor_rejects_malformed_types(tmp_path, monkeypatch, data):
    vendor = load_script("vendor")
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data))
    monkeypatch.setattr(vendor, "MANIFEST", path)
    with pytest.raises(ValueError):
        vendor.read_manifest()


def test_vendor_download_parallel_and_atomic(tmp_path, monkeypatch):
    import threading

    vendor = load_script("vendor")
    barrier = threading.Barrier(2)
    raw = b"export const value = 1;"

    def download(*args, **kwargs):
        barrier.wait(timeout=5)
        return io.BytesIO(raw)

    monkeypatch.setattr(urllib.request, "urlopen", download)
    monkeypatch.setattr(vendor, "MANIFEST", manifest(tmp_path, [vendor_file("a.js"), vendor_file("b.js")]))
    monkeypatch.setenv("LIB_DIR", str(tmp_path / "lib"))
    monkeypatch.setattr(sys, "argv", ["vendor.py"])
    vendor.main()
    assert (tmp_path / "lib/a.js").read_bytes() == raw
    assert (tmp_path / "lib/b.js").read_bytes() == raw
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"corrupt"))
    with pytest.raises(ValueError, match="sha256"):
        vendor.main()
    assert (tmp_path / "lib/a.js").read_bytes() == raw
    assert sorted(p.name for p in (tmp_path / "lib").iterdir()) == ["a.js", "b.js"]


def source_tar(tmp_path, members):
    path = tmp_path / "source.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, raw in members.items():
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
    return path


def intercept_exec(monkeypatch, bootstrap, work):
    calls = []
    monkeypatch.chdir(work)
    monkeypatch.setenv("PORT", "8080")
    monkeypatch.delenv("SVA_SPLIT_DIR", raising=False)
    monkeypatch.delenv("ASSET_SYNC", raising=False)
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(bootstrap.os, "execvp", lambda name, args: calls.append((name, args)))
    return calls


@pytest.mark.parametrize("sync", [False, True])
def test_bootstrap_file_tarball_until_exec(tmp_path, monkeypatch, sync):
    bootstrap = load_script("bootstrap")
    archive = source_tar(tmp_path, {"repo-sha/sva/app/requirements.txt": b"fastapi\n",
                                   "repo-sha/sva/app/scripts/vendor.py": b"# vendor\n",
                                   "repo-sha/sva/app/main.py": b"# app\n",
                                   "repo-sha/unrelated.txt": b"not extracted"})
    urlopen = urllib.request.urlopen
    urls = []

    def local_download(url, **kwargs):
        urls.append(url)
        return urlopen(archive.as_uri(), **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", local_download)
    monkeypatch.setenv("SVA_REPO", "team/project")
    monkeypatch.setenv("SVA_COMMIT", "a" * 40)
    monkeypatch.setenv("SVA_TARBALL_SHA256", hashlib.sha256(archive.read_bytes()).hexdigest())
    work = tmp_path / "state"
    work.mkdir()
    calls = intercept_exec(monkeypatch, bootstrap, work)
    if sync:
        monkeypatch.setenv("ASSET_SYNC", "1")
        monkeypatch.setenv("ASSET_DIR", "/state/assets")
    bootstrap.main()
    assert urls == [f"https://github.com/team/project/archive/{'a' * 40}.tar.gz"]
    assert Path.cwd() == work / "sva"
    assert (Path.cwd() / "app/main.py").exists()
    assert not (work / "unrelated.txt").exists()
    assert calls[0][0] == [sys.executable, "-m", "pip", "install", "--no-cache-dir", "-r", "app/requirements.txt"]
    assert calls[1][0] == [sys.executable, "app/scripts/vendor.py"]
    if sync:
        assert calls.pop(2) == ([sys.executable, "pipeline/common/upload.py", "--download", "/state/assets"], {"check": True})
    assert calls[2:] == [("uvicorn", ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"])]


def test_bootstrap_bad_hash_stops_before_install(tmp_path, monkeypatch):
    bootstrap = load_script("bootstrap")
    archive = source_tar(tmp_path, {"repo/sva/app/main.py": b"# app"})
    urlopen = urllib.request.urlopen
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: urlopen(archive.as_uri()))
    monkeypatch.setenv("SVA_REPO", "team/project")
    monkeypatch.setenv("SVA_COMMIT", "a" * 40)
    monkeypatch.setenv("SVA_TARBALL_SHA256", "0" * 64)
    work = tmp_path / "state"
    work.mkdir()
    calls = intercept_exec(monkeypatch, bootstrap, work)
    with pytest.raises(ValueError, match="sha256"):
        bootstrap.main()
    assert not calls
    assert not (work / "sva/app/main.py").exists()


def test_configmap_bootstrap_size():
    result = subprocess.run(["bash", str(SCRIPTS / "configmap_size.sh")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    total = (SCRIPTS / "bootstrap.py").stat().st_size + (SVA / "app/requirements.txt").stat().st_size
    assert f"{total} bytes" in result.stdout
    assert total < LIMIT


def test_configmap_rejects_900_kib(tmp_path):
    scripts = tmp_path / "app/scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(SCRIPTS / "configmap_size.sh", scripts / "configmap_size.sh")
    (scripts / "bootstrap.py").write_bytes(b"x" * LIMIT)
    (tmp_path / "app/requirements.txt").write_bytes(b"")
    result = subprocess.run(["bash", str(scripts / "configmap_size.sh")], capture_output=True, text=True)
    assert result.returncode != 0
    assert "TOO LARGE" in result.stdout


def test_split_configmaps_roundtrip_without_network(tmp_path, monkeypatch):
    scripts = tmp_path / "build/app/scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(SCRIPTS / "configmap_size.sh", scripts / "configmap_size.sh")
    (scripts.parent / "requirements.txt").write_bytes(b"fastapi\n")
    raw = os.urandom(1500000)
    (scripts.parent / "main.py").write_bytes(raw)
    generated = scripts.parent / "static/lib"
    generated.mkdir(parents=True)
    (generated / "large.js").write_bytes(raw)
    build = tmp_path / "build"
    for rel in ["schemas/index.sql", "pipeline/__init__.py", "pipeline/common/__init__.py", "pipeline/common/site_map.py",
                "pipeline/fleet/pie.py"]:
        (build / rel).parent.mkdir(parents=True, exist_ok=True)
        (build / rel).write_text(f"# {rel}\n")
    result = subprocess.run(["bash", str(scripts / "configmap_size.sh"), "--split"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    documents = [part.strip() for part in result.stdout.split("---") if part.strip()]
    assert len(documents) > 1
    split = tmp_path / "split"
    for i, document in enumerate(documents):
        assert len(document.encode()) < LIMIT
        data = json.loads(document)
        mount = split / f"mount-{len(documents) - i}"
        mount.mkdir(parents=True)
        for name, value in data["binaryData"].items():
            (mount / name).write_bytes(base64.b64decode(value))
        hidden = mount / "..timestamp"
        hidden.mkdir()
        shutil.copyfile(mount / name, hidden / name)
    bootstrap = load_script("bootstrap")
    work = tmp_path / "state"
    work.mkdir()
    calls = intercept_exec(monkeypatch, bootstrap, work)
    monkeypatch.setenv("SVA_SPLIT_DIR", str(split))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("split went online"))
    bootstrap.main()
    assert (work / "sva/app/main.py").read_bytes() == raw
    assert not (work / "sva/app/static/lib/large.js").exists()
    # site mode imports schemas/*.sql and pipeline.common.site_map; the rest of pipeline/ stays out
    for rel in ["schemas/index.sql", "pipeline/__init__.py", "pipeline/common/__init__.py", "pipeline/common/site_map.py"]:
        assert (work / "sva" / rel).read_text() == f"# {rel}\n"
    assert not (work / "sva/pipeline/fleet/pie.py").exists()
    assert calls[-1][0] == "uvicorn"


def test_bootstrap_supports_public_repository_root_layout(tmp_path,monkeypatch):
    bootstrap=load_script('bootstrap')
    archive=source_tar(tmp_path,{'Spatial-Video-Agent-sha/app/requirements.txt':b'fastapi\n',
                               'Spatial-Video-Agent-sha/app/scripts/vendor.py':b'# vendor\n',
                               'Spatial-Video-Agent-sha/app/main.py':b'# app\n',
                               'Spatial-Video-Agent-sha/schemas/cosmos.json':b'{}'})
    original=urllib.request.urlopen
    monkeypatch.setattr(urllib.request,'urlopen',lambda url,**kw:original(archive.as_uri(),**kw))
    monkeypatch.setenv('SVA_REPO','Paradox07127/Spatial-Video-Agent')
    monkeypatch.setenv('SVA_COMMIT','a'*40)
    monkeypatch.setenv('SVA_TARBALL_SHA256',hashlib.sha256(archive.read_bytes()).hexdigest())
    work=tmp_path/'state'
    work.mkdir()
    intercept_exec(monkeypatch,bootstrap,work)
    bootstrap.main()
    assert (work/'sva/app/main.py').is_file()
    assert (work/'sva/schemas/cosmos.json').is_file()
