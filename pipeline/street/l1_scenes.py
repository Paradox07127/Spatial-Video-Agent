"""Street (L1) scenes from the live VSS rows: one scene per street camera and source video, its 30 s chunks in stream order.

  python pipeline/street/l1_scenes.py [--sva-out DIR] [--cache DIR] [--rows-json PATH]

Reads the live rows like site_map.py (one select; --rows-json reads a saved JSON list of those rows instead) and keeps the
rows site_map.scene_of puts in the street family. Writes <sva-out>/<scene_key>/manifest.json (workspace street, one cam2d
camera; a chunk spans [chunk start, chunk start + its last segment end) in stream time, uri = the s3:// chunk) and the VSS
caption of every 5 s segment to <cache>/<scene_key>/captions.jsonl, which index_build.py reads. The rows carry no frame size
or rate, so they come from SIZE (ffprobe of the chunks, .notes/evidence/2026-10-09-new-sources/vm-probe.md Q2). dets/ is left
empty: the app reads 2D boxes from VSS at query time.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common import paths, site_map  # noqa: E402
from pipeline.common.index_build import CLIP_MS  # noqa: E402

OUT_ROOT = paths.OUT
ANALYSIS_REV = "l1-1"
# camera base -> (w, h, fps) of its VSS chunks
SIZE = {"neighborhood_cam": (2048, 1154, 19.9)}
DEFAULT_SIZE = (1920, 1080, 30)
CAPTION_MODEL = "nvidia/cosmos3-nano-reasoner"
NAMES = {"nyc_bike_gopro": "New York bike GoPro", "nyc_streets_cam": "New York street camera",
         "sf_streets_cam": "San Francisco street camera", "neighborhood_cam": "Neighborhood camera"}


def scene(key: str, segs: list[tuple[tuple[str, str, int], dict]]) -> tuple[dict, list[dict]]:
    """(manifest, caption lines) of one street scene from its (scene_of, row) pairs; ValueError unless the chunks tile
    [0, t1_ms)."""
    (_, cam, _), first = segs[0]
    stem = site_map.CHUNK.match(first["original_video"].rsplit("/", 1)[-1])[1]
    starts, ends = {}, {}
    for (_, _, t_ms), r in segs:
        starts[r["original_video"]] = t_ms - round(r["segment_start_sec"] * 1000)
        ends[r["original_video"]] = max(ends.get(r["original_video"], 0), r["segment_end_sec"])
    chunks = sorted(({"t0_ms": t0, "t1_ms": t0 + round(ends[uri] * 1000), "uri": uri} for uri, t0 in starts.items()),
                    key=lambda c: c["t0_ms"])
    t1 = 0
    for c in chunks:
        if c["t0_ms"] != t1:
            raise ValueError(f"{key}: {c['uri']} starts at {c['t0_ms']} ms, the chunks before it end at {t1} ms")
        t1 = c["t1_ms"]
    base, n = cam.rsplit("-", 1)
    w, h, fps = SIZE.get(base, DEFAULT_SIZE)
    m = {"schema_version": "1", "analysis_rev": ANALYSIS_REV, "producer_commit": os.environ.get("SVA_PRODUCER_COMMIT", "unknown"),
         "input_artifact_hashes": {}, "scene_id": "/".join(key.split("-", 2)), "scene_key": key, "workspace": "street",
         "evidence_level": "L1", "title": f"{NAMES[base]} {n} ({stem})", "t0_ms": 0, "t1_ms": t1, "fps": fps,
         "world_frame_id": "image", "metric": False,
         "cams": [{"kind": "cam2d", "id": cam, "w": w, "h": h, "video": None, "chunks": chunks,
                   "frame_ref": {"video_w": w, "video_h": h, "thumb_w": 320, "thumb_h": round(320 * h / w)}, "offset_ms": 0, "thumbs": None,
                   "sync_method": "single_camera", "sync_residual_ms": None}],
         "assets": {"dets_dir": "dets"},
         "sources": {"vss": {"camera_id": first["camera_id"], "location": first["location"], "stem": stem}},
         "pipeline": {"models": {"detector": "yolo11_coco (VSS sidecar)", "caption": f"{CAPTION_MODEL} (VSS)"},
                      "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}}
    caps = [{"clip_id": f"{key}:{cam}:{t_ms // CLIP_MS:05d}", "caption": r["reasoning_content"], "prompt_rev": "vss",
             "model_rev": CAPTION_MODEL, "analyzed_ms": None} for (_, _, t_ms), r in sorted(segs, key=lambda s: s[0][2])]
    return m, caps


def scenes(rows: list[dict]) -> dict[str, tuple[dict, list[dict]]]:
    """scene_key -> (manifest, caption lines) for every street scene in the live rows."""
    by_key: dict[str, list] = {}
    for r in rows:
        family, sc = site_map.scene_of(r, {})
        if family == "street":
            by_key.setdefault(sc[0], []).append((sc, r))
    return {key: scene(key, segs) for key, segs in sorted(by_key.items())}


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sva-out", default=str(OUT_ROOT))
    ap.add_argument("--cache", default=str(OUT_ROOT / "_tmp" / "P-C"), help="caption cache root that index_build.py reads")
    ap.add_argument("--rows-json", default=None, help="saved JSON list of live rows instead of reading VastDB")
    a = ap.parse_args()
    rows = json.loads(Path(a.rows_json).read_text()) if a.rows_json else site_map.vastdb_rows()
    built = scenes(rows)
    for key, (m, caps) in built.items():
        write(Path(a.sva_out) / key / "manifest.json", json.dumps(m))
        (Path(a.sva_out) / key / "dets").mkdir(exist_ok=True)
        write(Path(a.cache) / key / "captions.jsonl", "".join(json.dumps(c) + "\n" for c in caps))
    print(f"l1_scenes: {len(built)} street scenes, {sum(len(m['cams'][0]['chunks']) for m, _ in built.values())} chunks, "
          f"{sum(len(caps) for _, caps in built.values())} segments from {len(rows)} live rows -> {a.sva_out}", file=sys.stderr)
    for key, (m, caps) in built.items():
        print(f"  {key}: chunks={len(m['cams'][0]['chunks'])} segments={len(caps)} t1_ms={m['t1_ms']}", file=sys.stderr)


if __name__ == "__main__":
    main()
