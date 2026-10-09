"""Host-wide resource lease on ws (one fcntl-locked state file under LEASE_ROOT).

Totals are per host, across all processes: CPU tokens 48, GPU pipeline holders 1, concurrent requests to :8101/:8103 8.
Every pipeline script acquires before heavy work; acquire() also sets OMP/MKL/OPENBLAS thread counts to the CPU tokens held
(effective for libraries imported afterwards). Holders whose pid is gone are reaped, so a killed job frees its tokens.
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common import paths  # noqa: E402

CPU_TOTAL = 48
GPU_HOLDERS = 1
VLM_CONCURRENCY = 8
LEASE_ROOT = paths.OUT / "_lease"
POLL_S = 0.5


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextmanager
def _state(root: Path) -> Iterator[dict]:
    root.mkdir(parents=True, exist_ok=True)
    with open(root / "lock", "a+") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        path = root / "state.json"
        st = json.loads(path.read_text()) if path.exists() and path.stat().st_size else {"holders": {}}
        st["holders"] = {k: h for k, h in st["holders"].items() if _alive(h["pid"])}
        yield st
        tmp = root / "state.json.tmp"
        tmp.write_text(json.dumps(st))
        os.replace(tmp, path)


def _take(root: Path, owner: str, cpu: int, gpu: int, vlm: int, timeout_s: float | None) -> str:
    if cpu > CPU_TOTAL or gpu > GPU_HOLDERS or vlm > VLM_CONCURRENCY:
        raise ValueError(f"request cpu={cpu} gpu={gpu} vlm={vlm} exceeds host totals")
    deadline = None if timeout_s is None else time.monotonic() + timeout_s
    while True:
        with _state(root) as st:
            hs = st["holders"].values()
            if (sum(h["cpu"] for h in hs) + cpu <= CPU_TOTAL and sum(h["gpu"] for h in hs) + gpu <= GPU_HOLDERS
                    and sum(h["vlm"] for h in hs) + vlm <= VLM_CONCURRENCY):
                hid = uuid.uuid4().hex[:12]
                st["holders"][hid] = {"pid": os.getpid(), "owner": owner, "cpu": cpu, "gpu": gpu, "vlm": vlm, "t": time.time()}
                return hid
        if deadline is not None and time.monotonic() > deadline:
            raise TimeoutError(f"lease not granted within {timeout_s} s: owner={owner} cpu={cpu} gpu={gpu} vlm={vlm}")
        time.sleep(POLL_S)


def _drop(root: Path, hid: str) -> None:
    with _state(root) as st:
        st["holders"].pop(hid, None)


class Lease:
    def __init__(self, root: Path, hid: str, owner: str, cpu: int, gpu: int):
        self.root, self.hid, self.owner, self.cpu, self.gpu = root, hid, owner, cpu, gpu
        self._released = False

    def release(self) -> None:
        if not self._released:
            _drop(self.root, self.hid)
            self._released = True
            print(f"lease release owner={self.owner} cpu={self.cpu} gpu={self.gpu}", file=sys.stderr)

    def __enter__(self) -> "Lease":
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def acquire(cpu: int, gpu: int = 0, *, owner: str, timeout_s: float | None = None, root: Path = LEASE_ROOT) -> Lease:
    """Blocks until cpu tokens and gpu holder slots are free (None = wait forever); logs `lease acquire owner=.. cpu=.. gpu=..`."""
    hid = _take(root, owner, cpu, gpu, 0, timeout_s)
    for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        os.environ[var] = str(max(cpu, 1))
    print(f"lease acquire owner={owner} cpu={cpu} gpu={gpu}", file=sys.stderr)
    return Lease(root, hid, owner, cpu, gpu)


@contextmanager
def vlm_slot(*, owner: str, root: Path = LEASE_ROOT) -> Iterator[None]:
    """One of VLM_CONCURRENCY slots for a single request to :8101 or :8103."""
    hid = _take(root, owner, 0, 0, 1, None)
    try:
        yield
    finally:
        _drop(root, hid)


def status(root: Path = LEASE_ROOT) -> dict:
    """Current holders and totals (python lease.py prints it)."""
    with _state(root) as st:
        hs = list(st["holders"].values())
    return {"cpu": sum(h["cpu"] for h in hs), "gpu": sum(h["gpu"] for h in hs), "vlm": sum(h["vlm"] for h in hs), "holders": hs}


if __name__ == "__main__":
    print(json.dumps(status(Path(sys.argv[1]) if len(sys.argv) > 1 else LEASE_ROOT), indent=1))
