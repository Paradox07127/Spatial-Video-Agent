import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request


def main():
    work = Path.cwd()
    archive = work / "source.tar.gz"
    source = work / "sva"
    split = os.environ.get("SVA_SPLIT_DIR")
    if split:
        root = Path(split)
        parts = sorted((p for p in root.rglob("source.part-*")
                        if not any(s.startswith(".") for s in p.relative_to(root).parts)), key=lambda p: p.name)
        if not parts:
            raise ValueError("no source parts in SVA_SPLIT_DIR")
        with archive.open("wb") as dest:
            for part in parts:
                with part.open("rb") as src:
                    shutil.copyfileobj(src, dest)
    else:
        repo, commit, expected = (os.environ[key] for key in
                                  ("SVA_REPO", "SVA_COMMIT", "SVA_TARBALL_SHA256"))
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("SVA_REPO must be owner/repository")
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("SVA_COMMIT must be a full commit SHA")
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("SVA_TARBALL_SHA256 must be a sha256 digest")
        url = f"https://github.com/{repo}/archive/{commit}.tar.gz"
        with urllib.request.urlopen(url, timeout=60) as response, archive.open("wb") as dest:
            shutil.copyfileobj(response, dest)
        with archive.open("rb") as src:
            if hashlib.file_digest(src, "sha256").hexdigest() != expected:
                raise ValueError("source tarball sha256 mismatch")
    with tarfile.open(archive, "r:gz") as tar:
        members = []
        for member in tar.getmembers():
            if not split:
                path = PurePosixPath(member.name)
                if len(path.parts) < 3 or path.parts[1] != "sva":
                    continue
                member.name = str(PurePosixPath(*path.parts[2:]))
            members.append(member)
        tar.extractall(source, members=members, filter="data")
    os.chdir(source)
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-cache-dir", "-r", "app/requirements.txt"], check=True)
    subprocess.run([sys.executable, "app/scripts/vendor.py"], check=True)
    if os.environ.get("ASSET_SYNC") == "1":
        # browsers cannot reach the internal S3 endpoint, so the app serves the scene assets from this local mirror
        subprocess.run([sys.executable, "pipeline/common/upload.py", "--download", os.environ["ASSET_DIR"]], check=True)
    print("bootstrap: ready to exec uvicorn", flush=True)
    os.execvp("uvicorn", ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", os.environ.get("PORT", "8080")])


if __name__ == "__main__":
    main()
