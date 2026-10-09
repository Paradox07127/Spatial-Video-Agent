"""index_build rows and VastDB writer against an in-memory fake session.  .venv/bin/python -m pytest pipeline/common/test_index_build.py -q"""
import hashlib
import json
import sys
import types
from contextlib import contextmanager

import duckdb
import pyarrow as pa

from pipeline.common import index_build


class FakeTable:
    def __init__(self, db, name, schema):
        self.db, self.name, self.data = db, name, schema.empty_table()

    def insert(self, t):
        self.data = pa.concat_tables([self.data, t])

    def drop(self):
        del self.db.tables[self.name]

    def select(self, columns):
        self.db.opened.append(self.name)
        return types.SimpleNamespace(read_all=lambda: self.data.select(columns))


class FakeSchema:
    def __init__(self, db):
        self.db = db

    def table(self, name, fail_if_missing=True):
        if name not in self.db.tables and fail_if_missing:
            raise KeyError(name)
        return self.db.tables.get(name)

    def create_table(self, name, schema):
        self.db.tables[name] = FakeTable(self.db, name, schema)
        return self.db.tables[name]


class FakeDB:
    def __init__(self):
        self.tables, self.opened = {}, []
        schema = FakeSchema(self)
        bucket = types.SimpleNamespace(schema=lambda name, fail_if_missing=True: schema, create_schema=lambda name: schema)
        self.tx = types.SimpleNamespace(bucket=lambda name: bucket)

    @contextmanager
    def transaction(self):
        yield self.tx


def test_every_table_is_recreated_and_read_back_even_when_empty(monkeypatch):
    con = duckdb.connect()
    con.execute((index_build.SVA / "schemas/index.sql").read_text())
    con.execute("INSERT INTO places VALUES ('pl_a', 'road', 'A St', [], 43.6, -79.4, NULL)")
    db = FakeDB()
    for name in index_build.TABLES:  # a previous build left every table with stale rows
        db.tables[name] = FakeTable(db, name, pa.schema([("x", pa.int64())]))
        db.tables[name].insert(pa.table({"x": [1, 2]}))
    monkeypatch.setitem(sys.modules, "vastdb", types.SimpleNamespace(connect=lambda **kw: db))
    for k in ("S3_ENDPOINT", "ACCESS_KEY", "SECRET_KEY", "VASTDB_BUCKET"):
        monkeypatch.setenv(k, "x")
    index_build.write_vastdb(con)
    assert set(db.tables) == set(index_build.TABLES)
    assert {n: db.tables[n].data.num_rows for n in index_build.TABLES} == {n: int(n == "places") for n in index_build.TABLES}
    assert sorted(db.opened) == sorted(index_build.TABLES)


def test_vectors_are_fixed_size_lists_of_non_null_float32():
    con = duckdb.connect()
    con.execute((index_build.SVA / "schemas/index.sql").read_text())
    con.execute("INSERT INTO clips (clip_id, v_txt) VALUES ('a', ?), ('b', NULL)", [[0.5] * 256])
    t = index_build.arrow_of(con, "clips")
    vec = pa.list_(pa.field("item", pa.float32(), nullable=False), 256)
    assert (t.schema.field("v_txt").type, t.schema.field("v_vis").type) == (vec, vec)
    assert t.column("v_txt").to_pylist() == [[0.5] * 256, None]
    assert str(t.schema.field("zone_ids").type) == "list<item: string>"  # a list child named "l" is a 500


def test_from_duckdb_loads_a_built_index_without_rebuilding(tmp_path, monkeypatch, capsys):
    path = tmp_path / "index.duckdb"
    con = duckdb.connect(str(path))
    con.execute((index_build.SVA / "schemas/index.sql").read_text())
    con.execute("INSERT INTO clips (clip_id, v_txt) VALUES ('a', ?)", [[0.5] * 256])
    con.close()
    db = FakeDB()
    monkeypatch.setitem(sys.modules, "vastdb", types.SimpleNamespace(connect=lambda **kw: db))
    for k in ("S3_ENDPOINT", "ACCESS_KEY", "SECRET_KEY", "VASTDB_BUCKET"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setattr(sys, "argv", ["index_build.py", "--from-duckdb", str(path), "--sva-out", str(tmp_path / "none")])
    index_build.main()
    assert {n: db.tables[n].data.num_rows for n in index_build.TABLES} == {n: int(n == "clips") for n in index_build.TABLES}
    assert "vastdb sva.clips: wrote 1 read back 1 OK" in capsys.readouterr().out


def built(sva_out, cache, vss):
    con = duckdb.connect()
    con.execute((index_build.SVA / "schemas/index.sql").read_text())
    places = {}
    for sdir in index_build.scene_dirs(sva_out):
        index_build.build(con, sdir, cache, vss, places)
    index_build.insert(con, "places", list(places.values()))
    return con


def test_warehouse_and_fleet_rows_are_unchanged(tmp_path):
    """Digest of every table but scene_manifest for fixtures/sva-out, taken with index_build before street scenes existed."""
    con = built(index_build.SVA / "fixtures/sva-out", tmp_path, {})
    rows = {t: con.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall() for t in index_build.TABLES if t != "scene_manifest"}
    assert hashlib.sha256(repr(rows).encode()).hexdigest() == "5739c15e3bf78ad5e567cb820ddc2ae8bbce9759385b81fe26919978ddedfdb5"


def test_street_scene_has_one_clip_per_vss_segment(tmp_path):
    key, cam = "st-sf_streets_cam_4-sf4", "sf_streets_cam-4"
    uri = "s3://team-38-vss-chunks/team-38/20261001_060000_sf4_chunk_{:04d}.mp4"
    m = {"scene_id": "st/sf_streets_cam_4/sf4", "scene_key": key, "workspace": "street", "title": "sf4", "t0_ms": 0, "t1_ms": 42500,
         "cams": [{"kind": "cam2d", "id": cam, "video": None,
                   "chunks": [{"t0_ms": 0, "t1_ms": 30000, "uri": uri.format(0)}, {"t0_ms": 30000, "t1_ms": 42500, "uri": uri.format(1)}]}],
         "assets": {"dets_dir": "dets"}}
    (tmp_path / "out" / key).mkdir(parents=True)
    (tmp_path / "out" / key / "manifest.json").write_text(json.dumps(m))
    (tmp_path / "cache" / key).mkdir(parents=True)
    (tmp_path / "cache" / key / "captions.jsonl").write_text(json.dumps({"clip_id": f"{key}:{cam}:00007", "caption": "a bus"}) + "\n")
    vss = {"s3://seg-7.mp4": {"source": "s3://seg-7.mp4", "clip_id": f"{key}:{cam}:00007", "upload_ms": 1790840712000}}
    con = built(tmp_path / "out", tmp_path / "cache", vss)
    clips = con.execute("SELECT clip_id, workspace, cam_id, t0_ms, t1_ms, video_uri, n_person, n_vehicle FROM clips ORDER BY t0_ms").fetchall()
    assert [c[3:5] for c in clips] == [(t, t + 5000) for t in range(0, 40000, 5000)] + [(40000, 42500)]
    assert {c[1:3] for c in clips} == {("street", cam)}
    assert [c[5] for c in clips] == [uri.format(0)] * 6 + [uri.format(1)] * 3
    assert {c[6:8] for c in clips} == {(None, None)}
    assert con.execute(f"SELECT vss_source, upload_ms, caption FROM clips WHERE clip_id = '{key}:{cam}:00007'").fetchone() == (
        "s3://seg-7.mp4", 1790840712000, "a bus")
    assert con.execute("SELECT count(*) FROM clips WHERE caption IS NULL AND vss_source IS NULL").fetchone() == (8,)
