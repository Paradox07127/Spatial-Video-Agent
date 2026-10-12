"""Cosmos-Embed1 (:8103, 256-d) text and video vectors for the 5 s clips of published scenes.

  python pipeline/common/embed.py [--sva-out DIR] [--cache DIR] [--site-map PATH] [SCENE_KEY ...]

Appends {clip_id, kind: txt|vis, src, vec} to <cache>/<scene_key>/embeds.jsonl. txt embeds the clip caption from
captions.jsonl (src = caption sha256, re-embedded when the caption changes). vis embeds the VSS corpus segment covering the
same clip (site_map segment_path), or, for a clip no segment covers, a cut of the clip from the scene's own video written to
--clip-dir: the :8103 service reads only paths under the corpus data/ directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common import lease, paths, video  # noqa: E402
from pipeline.common.index_build import OUT_ROOT, read_jsonl, scene_clips  # noqa: E402
from pipeline.common.schema_check import scene_dirs  # noqa: E402

EMBED_URL = os.environ.get("EMBED_URL", "http://127.0.0.1:8103")
DIM = 256
BATCH = 8


def _post(path: str, body: dict) -> list[list[float]]:
    with lease.vlm_slot(owner="P-C"):
        r = httpx.post(f"{EMBED_URL}{path}", json=body, timeout=600)
    r.raise_for_status()
    vecs = r.json()["embeddings"]
    if any(len(v) != DIM for v in vecs):
        raise RuntimeError(f"{EMBED_URL}{path} returned {len(vecs[0])}-d vectors, expected {DIM} (wrong model?)")
    return vecs


def embed_texts(texts: list[str]) -> list[list[float]]:
    return _post("/embed/text", {"texts": texts})


def embed_videos(paths: list[str]) -> list[list[float]]:
    return _post("/embed/video", {"paths": paths})


def cut_clip(src: Path, media_ms: int, dur_ms: int, dst: Path) -> Path:
    """Re-encoded like the corpus segments, so frame-accurate whatever the source GOP."""
    if not dst.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(".tmp.mp4")
        video._run([*video.FF, "-ss", f"{media_ms / 1000:.3f}", "-i", str(src), "-t", f"{dur_ms / 1000:.3f}", "-an", "-c:v", "libx264",
                    "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-threads", "2", "-movflags", "+faststart", str(tmp)])
        tmp.replace(dst)
    return dst


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenes", nargs="*", help="scene keys (default: every published scene)")
    ap.add_argument("--sva-out", default=str(OUT_ROOT))
    ap.add_argument("--cache", default=str(OUT_ROOT / "_tmp" / "P-C"))
    ap.add_argument("--site-map", default=str(OUT_ROOT / "site_map.json"))
    ap.add_argument("--clip-dir", default=str(paths.DATA / "sva_clips"))
    a = ap.parse_args()
    seg = {r["clip_id"]: r["segment_path"] for r in json.loads(Path(a.site_map).read_text())["map"].values()
           if r["clip_id"] and r.get("segment_path")}
    n = {"txt": 0, "vis": 0}
    with lease.acquire(cpu=2, owner="P-C"):
        for sdir in scene_dirs(Path(a.sva_out)):
            if a.scenes and sdir.name not in a.scenes:
                continue
            cache = Path(a.cache) / sdir.name
            done = {(r["clip_id"], r["kind"]): r["src"] for r in read_jsonl(cache / "embeds.jsonl")}
            caps = {r["clip_id"]: r["caption"] for r in read_jsonl(cache / "captions.jsonl")}
            m = json.loads((sdir / "manifest.json").read_text())
            cams = {c["id"]: c for c in m["cams"]}
            spans = {c[0]: c[1:] for c in scene_clips(m)}
            own = {c: Path(a.clip_dir) / sdir.name / f"{c.split(':', 1)[1].replace(':', '-')}.mp4" for c in spans if c not in seg}
            jobs = [("txt", c, caps[c], hashlib.sha256(caps[c].encode()).hexdigest()[:16]) for c in spans if c in caps]
            jobs += [("vis", c, seg.get(c) or str(own[c]), seg.get(c) or str(own[c])) for c in spans]
            jobs = [j for j in jobs if done.get((j[1], j[0])) != j[3]]
            for kind in ("txt", "vis"):
                todo = [j for j in jobs if j[0] == kind]
                for i in range(0, len(todo), BATCH):
                    part = todo[i:i + BATCH]
                    for _, c, _, _ in part:
                        if kind == "vis" and c in own:
                            cam, t0, t1 = spans[c]
                            cut_clip(sdir / cams[cam]["video"], t0 - cams[cam]["offset_ms"], t1 - t0, own[c])
                    vecs = (embed_texts if kind == "txt" else embed_videos)([j[2] for j in part])
                    cache.mkdir(parents=True, exist_ok=True)
                    with (cache / "embeds.jsonl").open("a") as f:
                        for (_, clip_id, _, src), v in zip(part, vecs):
                            f.write(json.dumps({"clip_id": clip_id, "kind": kind, "src": src, "vec": v}) + "\n")
                    n[kind] += len(part)
    print(f"embed: {n['txt']} text and {n['vis']} video vectors written", file=sys.stderr)


if __name__ == "__main__":
    main()
