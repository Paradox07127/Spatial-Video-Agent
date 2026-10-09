import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from urllib.parse import urlsplit
import urllib.request


MANIFEST = Path(__file__).with_name("vendor_manifest.json")


def read_manifest():
    manifest = json.loads(MANIFEST.read_text())
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be an object")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("manifest files must be a nonempty list")
    names = set()
    for file in files:
        if not isinstance(file, dict) or not isinstance(file.get("name"), str):
            raise ValueError("manifest file must have a string name")
        name = file.get("name", "")
        path = PurePosixPath(name)
        if (not re.fullmatch(r"[A-Za-z0-9_./-]+", name) or path.is_absolute()
                or not path.parts or ".." in path.parts or str(path) != name or name in names):
            raise ValueError("invalid or duplicate manifest name")
        sha = file.get("sha256")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ValueError(f"{name}: invalid sha256")
        url = file.get("url")
        if not isinstance(url, str) or urlsplit(url).scheme != "https" or not urlsplit(url).hostname:
            raise ValueError(f"{name}: URL must use HTTPS")
        if type(file.get("size")) is not int or file["size"] <= 0:
            raise ValueError(f"{name}: size must be positive bytes")
        names.add(name)
    imports = manifest.get("importmap")
    if not isinstance(imports, dict) or not imports:
        raise ValueError("manifest importmap must be a nonempty object")
    for name, target in imports.items():
        if not isinstance(name, str) or not isinstance(target, str):
            raise ValueError("invalid importmap entry")
        target = target.removeprefix("./").removeprefix("lib/")
        if target not in names and not (target.endswith("/") and any(n.startswith(target) for n in names)):
            raise ValueError(f"{name}: importmap target is absent from files")
    return files


def check_hash(file, raw):
    if hashlib.sha256(raw).hexdigest() != file["sha256"]:
        raise ValueError(f"{file['name']}: sha256 mismatch")


def fetch(file, dest):
    with urllib.request.urlopen(file["url"], timeout=60) as response:
        raw = response.read()
    check_hash(file, raw)
    path = dest / file["name"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
        temp.write(raw)
    os.replace(temp.name, path)
    print(f"{file['name']}: {len(raw)} bytes, sha256 ok")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    files = read_manifest()
    dest = Path(os.environ.get("LIB_DIR", str(MANIFEST.parents[1] / "static/lib")))
    if args.check:
        local = 0
        for file in files:
            path = dest / file["name"]
            if path.exists():
                check_hash(file, path.read_bytes())
                local += 1
        print(f"vendor: {len(files)} files, {local} local hashes checked; OK (offline)")
    else:
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda file: fetch(file, dest), files))


if __name__ == "__main__":
    main()
