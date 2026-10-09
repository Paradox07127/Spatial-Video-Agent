"""Map the live VSS rows onto our scenes: chunk file name (+ PIE live identity) -> (scene_key, cam_id, t_ms).

  python pipeline/common/site_map.py [--pie-identity PATH] [--sva-out DIR] [--out PATH]

Reads VASTDB_BUCKET / VDB_SCHEMA / VDB_COLLECTION (VDB_ENDPOINT or S3_ENDPOINT, ACCESS_KEY, SECRET_KEY). Chunk files are
team-38/<YYYYMMDD_HHMMSS>_<stem>_chunk_NNNN.mp4; SDG and SmartSpaces stems name the scene and camera and t_ms is
extra_metadata.stream_position_sec; a PIE chunk is looked up in live_identity.json (video_id, offset_s of the chunk file); a
street camera_id (STREET) gives the L1 scene st-<camera_id with - as _>-<stem>, lowercased like every scene_key, camera
camera_id, t_ms as for SDG.
A row is a hit when its scene is published under --sva-out (manifest.json exists) and has that camera. Output: --out and
--out.gz, {"map": {segment source: row}, counts}; A5 calls load(path) once, then map_hit(maps, result): by source, or by (original_video, segment_start_sec) when
re-ingest moved the segment.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
from datetime import timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common import paths  # noqa: E402

OUT_ROOT = paths.OUT
PIE_IDENTITY = Path.home() / "sva-data/pie/live_identity.json"
COLUMNS = ["source", "original_video", "segment_start_sec", "segment_end_sec", "camera_id", "location", "extra_metadata",
           "upload_timestamp", "reasoning_content"]
CHUNK = re.compile(r"^\d{8}_\d{6}_(.+)_chunk_\d{4}\.mp4$")
SDG = re.compile(r"^[0-9a-f]{20}_([0-9a-f]{20})_run_(\d+)_seed_\d+\.(\w+)\.rgb$")
SMARTSPACES = re.compile(r"^2025_test_(Warehouse_\d+)_(Camera(?:_\d+)?)$")
PIE = re.compile(r"^set\d+_video$")
STREET = re.compile(r"^(nyc_bike_gopro|nyc_streets_cam|sf_streets_cam|neighborhood_cam)-\d+$")


def pie_index(identity: dict) -> dict:
    """live_identity.json -> {chunk s3 key (team-38/...mp4): chunk entry}."""
    return {c["s3_key"]: c for chunks in identity.values() for c in chunks}


def scene_of(row: dict, pie: dict) -> tuple[str, tuple[str, str, int] | None]:
    """(source family, (scene_key, cam_id, t_ms) or None) of a live row."""
    m = CHUNK.match(row["original_video"].rsplit("/", 1)[-1])
    stem = m[1] if m else ""
    pos = json.loads(row["extra_metadata"] or "{}").get("stream_position_sec")
    if m := SDG.match(stem):
        return "sdg", (f"wh-sdg-{m[1][:8]}_run_{m[2]}", m[3], round(pos * 1000))
    if m := SMARTSPACES.match(stem):
        return "smartspaces", (f"wh-ss-{m[1].lower()}", m[2].lower(), round(pos * 1000))
    if PIE.match(stem):
        c = pie.get(row["original_video"].split("/", 3)[-1])
        if c is None:
            return "pie", None
        return "pie", (f"fl-pie-{c['video_id'].replace('/', '_')}", "dashcam", round((c["offset_s"] + row["segment_start_sec"]) * 1000))
    if stem and STREET.match(cam := row["camera_id"].lower()):
        return "street", (f"st-{cam.replace('-', '_')}-{stem.lower()}", cam, round(pos * 1000))
    return "other", None


def vastdb_rows() -> list[dict]:
    import vastdb
    session = vastdb.connect(endpoint=os.environ.get("VDB_ENDPOINT") or os.environ["S3_ENDPOINT"], access=os.environ["ACCESS_KEY"],
                             secret=os.environ["SECRET_KEY"], ssl_verify=False)
    with session.transaction() as tx:
        table = tx.bucket(os.environ["VASTDB_BUCKET"]).schema(os.environ["VDB_SCHEMA"]).table(os.environ["VDB_COLLECTION"])
        return table.select(columns=COLUMNS).read_all().to_pylist()


def build(rows: list[dict], sva_out: Path, pie: dict) -> dict:
    cams: dict[str, set[str]] = {}
    for p in sva_out.glob("*/manifest.json"):
        cams[p.parent.name] = {c["id"] for c in json.loads(p.read_text())["cams"]}
    out, families = {}, {}
    for r in rows:
        family, sc = scene_of(r, pie)
        n = families.setdefault(family, {"rows": 0, "named": 0, "published": 0, "hit": 0})
        n["rows"] += 1
        base = {"source": r["source"], "original_video": r["original_video"], "camera_id": r["camera_id"],
                "segment_start_sec": r["segment_start_sec"], "family": family,
                "upload_ms": int(r["upload_timestamp"].replace(tzinfo=timezone.utc).timestamp() * 1000) if r["upload_timestamp"] else None}
        if sc is None:
            out[r["source"]] = {**base, "scene_key": None, "cam_id": None, "t_ms": None, "clip_id": None, "hit": False}
            continue
        key, cam, t_ms = sc
        hit = cam in cams.get(key, set())
        n["named"] += 1
        n["published"] += key in cams
        n["hit"] += hit
        out[r["source"]] = {**base, "scene_key": key, "cam_id": cam, "t_ms": t_ms, "clip_id": f"{key}:{cam}:{t_ms // 5000:05d}", "hit": hit}
    n_hit = sum(n["hit"] for n in families.values())
    return {"schema_version": "2", "n_rows": len(rows), "n_hit": n_hit, "map_rate": round(n_hit / max(len(rows), 1), 4),
            "n_published_scenes": len(cams), "families": families, "map": out}


def load(path: Path) -> tuple[dict, dict]:
    """(by source, by (original_video, segment_start_sec)) of mapped rows; path may be *.json or *.json.gz."""
    raw = Path(path).read_bytes()
    rows = [r for r in json.loads(gzip.decompress(raw) if str(path).endswith(".gz") else raw)["map"].values() if r["scene_key"]]
    return {r["source"]: r for r in rows}, {(r["original_video"], float(r["segment_start_sec"])): r for r in rows}


def map_hit(maps: tuple[dict, dict], hit: dict) -> dict | None:
    """A /search result (source, original_video, segment_start_sec) -> its row {scene_key, cam_id, t_ms, clip_id, ...}."""
    by_src, by_chunk = maps
    return by_src.get(hit["source"]) or by_chunk.get((hit["original_video"], float(hit["segment_start_sec"])))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pie-identity", default=str(PIE_IDENTITY))
    ap.add_argument("--sva-out", default=str(OUT_ROOT))
    ap.add_argument("--out", default=str(OUT_ROOT / "site_map.json"))
    a = ap.parse_args()
    doc = build(vastdb_rows(), Path(a.sva_out), pie_index(json.loads(Path(a.pie_identity).read_text())))
    data = json.dumps(doc).encode()
    for out, payload in ((a.out, data), (a.out + ".gz", gzip.compress(data))):  # .gz: the pod's ConfigMap binaryData
        tmp = Path(out + ".tmp")
        tmp.write_bytes(payload)
        tmp.replace(out)
    print(f"site_map: {doc['n_hit']}/{doc['n_rows']} live rows mapped to published cameras (map_rate {doc['map_rate']:.1%}); "
          f"{doc['n_published_scenes']} scenes published -> {a.out} (+ .gz)", file=sys.stderr)
    for family, n in sorted(doc["families"].items()):
        print(f"  {family}: rows={n['rows']} named={n['named']} on published scenes={n['published']} "
              f"hit={n['hit']} ({n['hit'] / n['rows']:.1%})", file=sys.stderr)
    by_scene: dict[tuple[str, bool], int] = {}
    for r in doc["map"].values():
        if r["scene_key"]:
            by_scene[r["scene_key"], r["hit"]] = by_scene.get((r["scene_key"], r["hit"]), 0) + 1
    for (key, hit), n in sorted(by_scene.items()):
        print(f"  {'hit' if hit else 'miss'} {key}: {n}", file=sys.stderr)


if __name__ == "__main__":
    main()
