"""Build index.duckdb from the published scenes (DDL schemas/index.sql, ids per CONTRACT §5) and swap it in atomically.

  python pipeline/common/index_build.py [--sva-out DIR] [--out PATH] [--cache DIR] [--site-map PATH] [--vastdb]
  python pipeline/common/index_build.py --from-duckdb PATH

Clips are 5 s per camera with video. A cam2d (street) camera has one clip per 5 s VSS segment of each chunk, video_uri = the
chunk uri, n_person/n_vehicle NULL. Captions and vectors come from the caches caption.py, embed.py and street/l1_scenes.py
write under <cache>/<scene_key>/ (absent -> NULL); vss_source, capture_ms and upload_ms come from site_map.json (absent -> NULL).
--vastdb also writes every table to VASTDB_BUCKET.sva (dropped and recreated) and reads the row counts back; without
VastDB credentials it prints the Arrow schemas only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import duckdb
import pyarrow as pa

SVA = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SVA))
from pipeline.common import paths  # noqa: E402
from pipeline.common.schema_check import scene_dirs  # noqa: E402

OUT_ROOT = paths.OUT
CLIP_MS = 5000
TABLES = ("scene_manifest", "clips", "detections", "tracks", "events", "gps", "places")


def scene_clips(m: dict) -> list[tuple[str, str, int, int]]:
    """(clip_id, cam_id, t0_ms, t1_ms) for every camera with video."""
    return [(f"{m['scene_key']}:{c['id']}:{t0 // CLIP_MS:05d}", c["id"], t0, min(t0 + CLIP_MS, m["t1_ms"]))
            for c in m["cams"] if c["video"] for t0 in range(m["t0_ms"], m["t1_ms"], CLIP_MS)]


def street_clips(m: dict) -> list[tuple[str, str, int, int, str]]:
    """(clip_id, cam_id, t0_ms, t1_ms, chunk uri) for every 5 s VSS segment of every chunk of a cam2d camera; VSS cuts each
    chunk from its start into 5 s segments, the last one shorter."""
    return [(f"{m['scene_key']}:{c['id']}:{t0 // CLIP_MS:05d}", c["id"], t0, min(t0 + CLIP_MS, ch["t1_ms"]), ch["uri"])
            for c in m["cams"] if c.get("kind") == "cam2d" for ch in c["chunks"] for t0 in range(ch["t0_ms"], ch["t1_ms"], CLIP_MS)]


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.is_file() else []


def load_json(sdir: Path, m: dict, key: str):
    return json.loads((sdir / m["assets"][key]).read_text()) if key in m["assets"] else None


def insert(con: duckdb.DuckDBPyConnection, table: str, rows: list[list]) -> None:
    """Through Arrow: executemany runs one statement per row."""
    if rows:
        cols = [d[0] for d in con.execute(f"SELECT * FROM {table} LIMIT 0").description]
        batch = pa.Table.from_pylist([dict(zip(cols, r)) for r in rows])  # noqa: F841 (read by the SQL below)
        con.execute(f"INSERT INTO {table} SELECT * FROM batch")


def build(con: duckdb.DuckDBPyConnection, sdir: Path, cache: Path, vss: dict, places: dict) -> None:
    """Inserts the scene's rows; adds its buildings and GPS place ids to places (first occurrence wins)."""
    m = json.loads((sdir / "manifest.json").read_text())
    key, sid = m["scene_key"], m["scene_id"]
    insert(con, "scene_manifest", [[sid, key, m["workspace"], m["title"], m["t0_ms"], m["t1_ms"], json.dumps(m)]])
    evs = (load_json(sdir, m, "events") or {"events": []})["events"]
    insert(con, "events", [[f"{key}:{e['id']}", sid, key, e["type"], e["t0_ms"], e["t1_ms"], e["actor_ids"], e.get("place_id"),
                           e["severity"], e["source"], json.dumps(e["params"]), e["clip_ids"]] for e in evs])
    trip = load_json(sdir, m, "trip")
    gps = [f["properties"] for f in trip["features"][1:]] if trip else []
    insert(con, "gps", [[key, p["t_ms"], p["lat"], p["lon"], p["heading"], p["speed"], p["way_id"], p["node_id"], p["place_id"],
                        p["state"], p["gap_ms"]] for p in gps])
    tracks = (load_json(sdir, m, "tracks") or {"tracks": []})["tracks"]
    insert(con, "tracks", [[f"{key}:{t['entity_id']}", sid, key, t["entity_id"], t["cls"], t["t0_ms"], t["t1_ms"], t["cam_ids"],
                           t["source"]] for t in tracks])
    world = {(t["entity_id"], p[0]): p[1:4] for t in tracks for p in t["pts"]}
    ents = (load_json(sdir, m, "entities") or {"entities": []})["entities"]
    owner = {(o["cam_id"], o["local_track_id"]): e["entity_id"] for e in ents for o in e["observations"]}
    rows: dict[str, list] = {c["id"]: [] for c in m["cams"]}
    if "boxes_dir" in m["assets"]:
        for f in sorted((sdir / m["assets"]["boxes_dir"]).glob("*/*.json")):
            rows[f.parent.name] += json.loads(f.read_text())["rows"]
    for cam, rs in rows.items():
        insert(con, "detections", [[sid, key, cam, r[0], r[2], r[7], r[3], r[4], r[5], r[6], r[1], ent,
                                    *world.get((ent, r[0]), (None,) * 3)] for r in rs for ent in [owner.get((cam, r[1]))]])

    caps = {r["clip_id"]: r for r in read_jsonl(cache / key / "captions.jsonl")}
    vecs = {(r["clip_id"], r["kind"]): r["vec"] for r in read_jsonl(cache / key / "embeds.jsonl")}
    by_clip = {v["clip_id"]: v for v in vss.values() if (v.get("clip_id") or "").startswith(key + ":")}
    video = {c["id"]: c["video"] for c in m["cams"]}
    out = []
    for clip_id, cam, t0, t1 in scene_clips(m):
        pts = [r for r in rows[cam] if t0 <= r[0] < t1]
        g = next((p for p in gps if t0 <= p["t_ms"] < t1), None)
        cap, v = caps.get(clip_id, {}), by_clip.get(clip_id, {})
        out.append([clip_id, sid, key, m["workspace"], cam, t0, t1, v.get("capture_ms"), v.get("upload_ms"), cap.get("analyzed_ms"),
                    v.get("source"), cap.get("caption"), vecs.get((clip_id, "txt")), vecs.get((clip_id, "vis")),
                    len({r[1] for r in pts if r[2] == "person"}), len({r[1] for r in pts if r[2] != "person"}),
                    *((g["lat"], g["lon"], g["heading"], g["speed"], g["place_id"]) if g else (None,) * 5), [], video[cam]])
    for clip_id, cam, t0, t1, uri in street_clips(m):
        cap, v = caps.get(clip_id, {}), by_clip.get(clip_id, {})
        out.append([clip_id, sid, key, m["workspace"], cam, t0, t1, v.get("capture_ms"), v.get("upload_ms"), cap.get("analyzed_ms"),
                    v.get("source"), cap.get("caption"), vecs.get((clip_id, "txt")), vecs.get((clip_id, "vis")),
                    None, None, *(None,) * 5, [], uri])
    insert(con, "clips", out)

    for p in (load_json(sdir, m, "places") or {"places": []})["places"]:  # named rows replace bare GPS place ids
        places[p["place_id"]] = [p["place_id"], p["kind"], p["name"], p["aliases"], p["lat"], p["lon"],
                                 json.dumps(p["geom"]) if p["geom"] else None]
    bl = load_json(sdir, m, "buildings")
    for bid, b in (bl["buildings"] if bl else {}).items():
        ring = b["geom"]["coordinates"][0][:-1]
        places.setdefault(bid, [bid, "building", b["name"], [], sum(p[1] for p in ring) / len(ring),
                                sum(p[0] for p in ring) / len(ring), json.dumps(b["geom"])])
    for p in gps:
        if p["place_id"] and p["place_id"] not in places:
            places.setdefault(p["place_id"], [p["place_id"], None, None, [], p["lat"], p["lon"], None])


def arrow_of(con: duckdb.DuckDBPyConnection, table: str) -> pa.Table:
    """VastDB side: vectors as fixed_size_list<float32 not null> (a nullable item type is rejected), lists with an "item"
    child, JSON as string."""
    t = con.execute(f"SELECT * FROM {table}").to_arrow_table()
    fields = []
    for f in t.schema:
        if pa.types.is_fixed_size_list(f.type):
            f = pa.field(f.name, pa.list_(pa.field("item", pa.float32(), nullable=False), f.type.list_size))
        elif pa.types.is_list(f.type):  # DuckDB names the child "l"; VastDB rejects that with a 500
            f = pa.field(f.name, pa.list_(f.type.value_type))
        elif not (pa.types.is_integer(f.type) or pa.types.is_floating(f.type)):
            f = pa.field(f.name, pa.string())
        fields.append(f)
    return t.cast(pa.schema(fields))


def write_vastdb(con: duckdb.DuckDBPyConnection) -> None:
    tables = {t: arrow_of(con, t) for t in TABLES}
    need = ("S3_ENDPOINT", "ACCESS_KEY", "SECRET_KEY", "VASTDB_BUCKET")
    if not all(os.environ.get(k) for k in need):
        print(f"vastdb dry run ({'/'.join(need)} not set): would write {os.environ.get('VASTDB_BUCKET', '<VASTDB_BUCKET>')}.sva")
        for name, t in tables.items():
            print(f"  sva.{name} rows={t.num_rows} schema: " + ", ".join(f"{f.name} {f.type}" for f in t.schema))
        return
    import vastdb
    session = vastdb.connect(endpoint=os.environ.get("VDB_ENDPOINT") or os.environ["S3_ENDPOINT"], access=os.environ["ACCESS_KEY"],
                             secret=os.environ["SECRET_KEY"], ssl_verify=False)
    with session.transaction() as tx:
        bucket = tx.bucket(os.environ["VASTDB_BUCKET"])
        schema = bucket.schema("sva", fail_if_missing=False) or bucket.create_schema("sva")
        for name, t in tables.items():
            old = schema.table(name, fail_if_missing=False)
            if old:
                old.drop()
            new = schema.create_table(name, t.schema)  # Store._load_vastdb needs every table, empty ones too
            if t.num_rows:
                new.insert(t)
    # a separate transaction: the writer can report success without rows landing
    with session.transaction() as tx:
        schema = tx.bucket(os.environ["VASTDB_BUCKET"]).schema("sva")
        bad = 0
        for name, t in tables.items():
            got = schema.table(name).select(columns=[t.schema[0].name]).read_all().num_rows
            bad += got != t.num_rows
            print(f"vastdb sva.{name}: wrote {t.num_rows} read back {got} {'OK' if got == t.num_rows else 'MISMATCH'}")
    if bad:
        sys.exit(f"vastdb: {bad} tables mismatched")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sva-out", default=str(OUT_ROOT))
    ap.add_argument("--out", default=None, help="default <sva-out>/index.duckdb")
    ap.add_argument("--cache", default=str(OUT_ROOT / "_tmp" / "P-C"))
    ap.add_argument("--site-map", default=None, help="default <sva-out>/site_map.json")
    ap.add_argument("--vastdb", action="store_true")
    ap.add_argument("--from-duckdb", default=None, help="write this built index to VastDB instead of building one")
    a = ap.parse_args()
    if a.from_duckdb:
        con = duckdb.connect(a.from_duckdb, read_only=True)
        write_vastdb(con)
        con.close()
        return
    root = Path(a.sva_out)
    out = Path(a.out or root / "index.duckdb")
    smap = Path(a.site_map or root / "site_map.json")
    vss = json.loads(smap.read_text())["map"] if smap.is_file() else {}
    tmp = out.with_suffix(".building.duckdb")
    tmp.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp))
    con.execute((SVA / "schemas/index.sql").read_text())
    scenes = scene_dirs(root)
    places: dict[str, list] = {}
    for sdir in scenes:
        build(con, sdir, Path(a.cache), vss, places)
    insert(con, "places", list(places.values()))
    counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}
    n_cap = con.execute("SELECT count(caption), count(v_txt), count(v_vis), count(vss_source) FROM clips").fetchone()
    if a.vastdb:
        write_vastdb(con)
    con.close()
    tmp.replace(out)
    print(f"index_build: {len(scenes)} scenes -> {out} " + " ".join(f"{k}={v}" for k, v in counts.items())
          + f" | clips with caption={n_cap[0]} v_txt={n_cap[1]} v_vis={n_cap[2]} vss_source={n_cap[3]}")


if __name__ == "__main__":
    main()
