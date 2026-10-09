"""Cosmos3 (:8101) captions for the 5 s clips of published scenes: 4 fps frames with the scene time drawn on, <= 8 in flight.

  python pipeline/common/caption.py [--sva-out DIR] [--cache DIR] [--prompt FILE] [SCENE_KEY ...]

Appends {clip_id, caption, prompt_rev, model_rev, analyzed_ms} to <cache>/<scene_key>/captions.jsonl; clips already
captioned with the same prompt_rev are skipped. The prompt is the fenced text of reingest_prompt.md plus a note about the
time overlay.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import cv2
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common import lease  # noqa: E402
from pipeline.common.index_build import OUT_ROOT, read_jsonl, scene_clips  # noqa: E402
from pipeline.common.schema_check import scene_dirs  # noqa: E402

VLM_URL = os.environ.get("VLM_URL", "http://127.0.0.1:8101/v1")
VLM_MODEL = os.environ.get("VLM_MODEL", "nvidia/cosmos3-nano-reasoner")
PROMPT_MD = Path(__file__).with_name("reingest_prompt.md")
FPS = 4
WIDTH = 640  # 20 frames at this width stay far inside the VLM's 32k-token context
IN_FLIGHT = 8
TIME_NOTE = "Each frame shows its scene time in seconds in the top-left corner; use those times when you say when something happens.\n"


def load_prompt(path: Path = PROMPT_MD) -> str:
    return re.search(r"```text\n(.*?)```", path.read_text(), re.S)[1].strip()


def prompt_rev(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def clip_video(src: Path, offset_ms: int, t0: int, t1: int) -> bytes | None:
    """MPEG-4 of scene time [t0, t1) at 4 fps with the time drawn on each frame; None when the camera has no frame there."""
    cap = cv2.VideoCapture(str(src))
    with tempfile.TemporaryDirectory() as d:
        out = None
        for t in range(max(t0, offset_ms), t1, 1000 // FPS):
            cap.set(cv2.CAP_PROP_POS_MSEC, t - offset_ms)
            ok, f = cap.read()
            if not ok:
                continue
            h = round(f.shape[0] * WIDTH / f.shape[1] / 2) * 2
            f = cv2.resize(f, (WIDTH, h))
            for color, thick in (((0, 0, 0), 4), ((255, 255, 255), 1)):
                cv2.putText(f, f"t={t / 1000:.2f}s", (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, thick)
            if out is None:
                out = cv2.VideoWriter(f"{d}/clip.mp4", cv2.VideoWriter_fourcc(*"mp4v"), FPS, (WIDTH, h))
            out.write(f)
        cap.release()
        if out is None:
            return None
        out.release()
        return Path(f"{d}/clip.mp4").read_bytes()


def caption(video: bytes, prompt: str) -> str:
    body = {"model": VLM_MODEL, "max_tokens": 1024, "temperature": 0.2,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "video_url", "video_url": {"url": "data:video/mp4;base64," + base64.b64encode(video).decode()}}]}]}
    with lease.vlm_slot(owner="P-C"):
        r = httpx.post(f"{VLM_URL}/chat/completions", json=body, timeout=600)
    r.raise_for_status()
    return (r.json()["choices"][0]["message"]["content"] or "").strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenes", nargs="*", help="scene keys (default: every published scene)")
    ap.add_argument("--sva-out", default=str(OUT_ROOT))
    ap.add_argument("--cache", default=str(OUT_ROOT / "_tmp" / "P-C"))
    ap.add_argument("--prompt", default=str(PROMPT_MD))
    a = ap.parse_args()
    prompt = TIME_NOTE + load_prompt(Path(a.prompt))
    rev = prompt_rev(prompt)
    jobs = []
    for sdir in scene_dirs(Path(a.sva_out)):
        if a.scenes and sdir.name not in a.scenes:
            continue
        m = json.loads((sdir / "manifest.json").read_text())
        done = {r["clip_id"] for r in read_jsonl(Path(a.cache) / sdir.name / "captions.jsonl") if r["prompt_rev"] == rev}
        cams = {c["id"]: c for c in m["cams"]}
        jobs += [(sdir.name, clip_id, sdir / cams[cam]["video"], cams[cam]["offset_ms"], t0, t1)
                 for clip_id, cam, t0, t1 in scene_clips(m) if clip_id not in done]
    print(f"caption: {len(jobs)} clips to caption, prompt_rev {rev}", file=sys.stderr)

    def one(job):
        key, clip_id, src, off, t0, t1 = job
        video = clip_video(src, off, t0, t1)
        return key, clip_id, (caption(video, prompt) if video else None)

    n_ok = n_fail = 0
    with lease.acquire(cpu=IN_FLIGHT, owner="P-C"), ThreadPoolExecutor(IN_FLIGHT) as ex:
        for fut in as_completed([ex.submit(one, j) for j in jobs]):
            try:
                key, clip_id, text = fut.result()
            except httpx.HTTPError as e:
                n_fail += 1
                print(f"  caption failed: {e}", file=sys.stderr)
                continue
            if text is None:
                continue
            path = Path(a.cache) / key / "captions.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as f:
                f.write(json.dumps({"clip_id": clip_id, "caption": text, "prompt_rev": rev, "model_rev": VLM_MODEL,
                                    "analyzed_ms": int(time.time() * 1000)}) + "\n")
            n_ok += 1
    print(f"caption: {n_ok} written, {n_fail} failed", file=sys.stderr)


if __name__ == "__main__":
    main()
