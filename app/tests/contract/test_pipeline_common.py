"""lease: host totals hold across processes and dead holders are reaped. publish: overlay, patch merge, no half-written scene."""
import json
import shutil
import subprocess
import sys
import time

import pytest

from pipeline.common import lease, publish

from .conftest import SVA, SVA_OUT, WH_KEY

HOLD = """
import sys, time
sys.path.insert(0, {sva!r})
from pathlib import Path
from pipeline.common import lease
l = lease.acquire({cpu}, {gpu}, owner="t", root=Path({root!r}))
print("held", flush=True)
time.sleep({hold})
l.release()
"""


def holder(tmp_path, cpu, gpu, hold):
    p = subprocess.Popen([sys.executable, "-c", HOLD.format(sva=str(SVA), cpu=cpu, gpu=gpu, root=str(tmp_path), hold=hold)],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    assert p.stdout.readline().strip() == "held"
    return p


def test_lease_blocks_until_release(tmp_path):
    p = holder(tmp_path, 40, 1, 3.0)
    with pytest.raises(TimeoutError):
        lease.acquire(10, owner="b", timeout_s=0.3, root=tmp_path)
    with pytest.raises(TimeoutError):
        lease.acquire(1, 1, owner="b", timeout_s=0.3, root=tmp_path)
    with lease.acquire(8, owner="small", timeout_s=0.3, root=tmp_path):  # 40 + 8 <= 48
        pass
    t0 = time.monotonic()
    with lease.acquire(10, 1, owner="b", timeout_s=5, root=tmp_path):
        assert time.monotonic() - t0 > 0.3
    p.wait()


def test_lease_reaps_dead_holder(tmp_path):
    p = holder(tmp_path, 48, 1, 30)
    p.kill()
    p.wait()
    with lease.acquire(48, 1, owner="after", timeout_s=2, root=tmp_path):
        assert lease.status(tmp_path)["cpu"] == 48


def test_vlm_slots_capped(tmp_path):
    ctxs = [lease.vlm_slot(owner="v", root=tmp_path) for _ in range(8)]
    for c in ctxs:
        c.__enter__()
    assert lease.status(tmp_path)["vlm"] == 8
    with pytest.raises(TimeoutError):
        lease._take(tmp_path, "v", 0, 0, 1, 0.2)
    for c in ctxs:
        c.__exit__(None, None, None)
    assert lease.status(tmp_path)["vlm"] == 0


@pytest.fixture
def out_root(tmp_path):
    src = publish.tmp_dir("P-W2", WH_KEY, out_root=tmp_path)
    shutil.copytree(SVA_OUT / WH_KEY, src, dirs_exist_ok=True)
    return tmp_path


def test_publish_then_patch(out_root):
    dst = publish.publish("P-W2", WH_KEY, out_root=out_root)
    assert (dst / "manifest.json").is_file() and not (out_root / f".{WH_KEY}.staging").exists()
    px = publish.tmp_dir("P-X", WH_KEY, out_root=out_root)
    (px / "cosmos").mkdir()
    shutil.copy(SVA_OUT / WH_KEY / f"cosmos/{WH_KEY}.json", px / "cosmos/extra.json")
    (px / "manifest.patch.json").write_text(json.dumps({"assets": {"cosmos": "cosmos/extra.json"}, "models": {"vlm": "x"}}))
    publish.publish("P-X", WH_KEY, out_root=out_root)
    m = json.loads((dst / "manifest.json").read_text())
    assert m["assets"]["cosmos"] == "cosmos/extra.json" and m["pipeline"]["models"]["vlm"] == "x"
    assert (dst / "points.bin").is_file() and not (dst / "manifest.patch.json").exists()


def test_publish_rejects_bad_scene_and_keeps_old(out_root):
    dst = publish.publish("P-W2", WH_KEY, out_root=out_root)
    before = (dst / "events.json").read_text()
    bad = publish.tmp_dir("P-W2", WH_KEY, out_root=out_root)
    ev = json.loads((bad / "events.json").read_text())
    ev["events"][0]["actor_ids"] = ["P99"]
    (bad / "events.json").write_text(json.dumps(ev))
    with pytest.raises(publish.PublishError):
        publish.publish("P-W2", WH_KEY, out_root=out_root)
    assert (dst / "events.json").read_text() == before
    assert not (out_root / f".{WH_KEY}.staging").exists()
