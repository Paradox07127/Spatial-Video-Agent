"""§6.4: DuckDB FLOAT[256] cosine works in this venv; the DDL loads; interval intersection and two-branch top-k run."""
import duckdb
import numpy as np

from .conftest import SVA


def test_array_cosine_similarity_float256():
    con = duckdb.connect()
    con.execute("CREATE TABLE t(id INT, v FLOAT[256])")
    rng = np.random.default_rng(0)
    vs = rng.normal(size=(5, 256)).astype(np.float32)
    for i, v in enumerate(vs):
        con.execute("INSERT INTO t VALUES (?, ?::FLOAT[256])", [i, v.tolist()])
    q = vs[3].tolist()
    rows = con.execute("SELECT id, array_cosine_similarity(v, ?::FLOAT[256]) AS s, array_cosine_distance(v, ?::FLOAT[256]) AS d "
                       "FROM t ORDER BY s DESC", [q, q]).fetchall()
    print(f"duckdb {duckdb.__version__}: top={rows[0]}")
    assert rows[0][0] == 3 and abs(rows[0][1] - 1) < 1e-5 and abs(rows[0][2]) < 1e-5
    ref = vs @ vs[3] / (np.linalg.norm(vs, axis=1) * np.linalg.norm(vs[3]))
    got = dict((i, s) for i, s, _ in rows)
    assert max(abs(got[i] - ref[i]) for i in range(5)) < 1e-5


def test_app_ddl_loads():
    con = duckdb.connect()
    con.execute((SVA / "schemas/app.sql").read_text())
    tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    assert {"agent_runs", "query_cache", "actions", "zones"} <= tables


def test_fixture_index(index_db):
    con = duckdb.connect(str(index_db), read_only=True)
    counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("clips", "detections", "tracks", "events", "gps", "places", "scene_manifest")}
    print(counts)
    assert all(counts.values())
    # interval intersection, not BETWEEN: E1 near_miss [4300, 5900) intersects [5000, 6000)
    ev = con.execute("SELECT event_id FROM events WHERE scene_key = ? AND t0_ms < ? AND t1_ms > ? ORDER BY event_id",
                     ["wh-sdg-fixture-01", 6000, 5000]).fetchall()
    assert [e[0] for e in ev] == ["wh-sdg-fixture-01:E1", "wh-sdg-fixture-01:E2"]
    q = con.execute("SELECT v_txt FROM clips WHERE clip_id = 'wh-sdg-fixture-01:ceiling_04:00000'").fetchone()[0]
    top = con.execute("""
        WITH t AS (SELECT clip_id, row_number() OVER (ORDER BY array_cosine_similarity(v_txt, ?::FLOAT[256]) DESC) AS r FROM clips),
             v AS (SELECT clip_id, row_number() OVER (ORDER BY array_cosine_similarity(v_vis, ?::FLOAT[256]) DESC) AS r FROM clips)
        SELECT clip_id, sum(1.0 / (60 + r)) AS rrf FROM (SELECT * FROM t WHERE r <= 50 UNION ALL SELECT * FROM v WHERE r <= 50)
        GROUP BY clip_id ORDER BY rrf DESC LIMIT 3""", [q, q]).fetchall()
    assert top[0][0] == "wh-sdg-fixture-01:ceiling_04:00000"
