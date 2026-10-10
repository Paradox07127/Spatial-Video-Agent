"""Evidence levels, capabilities and the scope default; the corpus ask fixtures parse into the contract models."""
import pytest
from pydantic import ValidationError

from app.core import models
from pipeline.common import schema_check as sc

from .conftest import FIXTURES, FL_KEY, WH_KEY, load

FL2_KEY = "fl-pie-fixture-02"
ST_KEY = "st-fixture_cam_1-clip01"
ANCHOR = "Show me every clip, from any camera in any site, where a person is close to a moving vehicle"


def manifest(key):
    return load(f"l1/{key}/manifest.json" if key.startswith("st-") else f"sva-out/{key}/manifest.json")


@pytest.mark.parametrize("key,level", [(WH_KEY, "L3"), (FL_KEY, "L2"), (FL2_KEY, "L2"), (ST_KEY, "L1")])
def test_level_of(key, level):
    assert models.level_of(manifest(key)) == level


def test_level_rules():
    st = manifest(ST_KEY)
    del st["evidence_level"]
    assert models.level_of(st) == "L1"
    wh = manifest(WH_KEY)
    assert models.level_of({**wh, "metric": False}) == "L2"
    assert models.level_of({**wh, "evidence_level": "L2"}) == "L2"


def _not_ok(m):
    return {cap: v["reason"] for cap, v in models.capabilities(m).items() if not v["ok"]}


def test_capabilities():
    assert set(models.ABSTAIN_TEXT) == {"no_geometry", "scene_not_metric", "no_track_ids", "no_gps", "no_visibility"}
    assert _not_ok(manifest(WH_KEY)) == {"speed": "no_gps"}
    assert _not_ok(manifest(FL_KEY)) == {"blind_spot": "no_visibility"}
    assert _not_ok(manifest(ST_KEY)) == {"distance": "no_geometry", "ground_position": "no_geometry", "map": "no_geometry",
                                         "blind_spot": "no_geometry", "tracks": "no_track_ids", "speed": "no_gps"}
    ss = manifest(WH_KEY)
    ss["metric"] = False
    del ss["assets"]["visibility"]
    assert _not_ok(ss) == {"distance": "scene_not_metric", "blind_spot": "no_visibility", "speed": "no_gps"}
    gps_only = _not_ok(manifest(FL2_KEY))
    assert gps_only["distance"] == "scene_not_metric" and gps_only["boxes_2d"] is None
    for key in (WH_KEY, FL_KEY, FL2_KEY, ST_KEY):
        caps = models.capabilities(manifest(key))
        assert list(caps) == list(models.CAPABILITIES)
        for v in caps.values():
            assert v["text"] == (models.ABSTAIN_TEXT[v["reason"]] if v["reason"] else None)


def test_query_scope():
    base = dict(template="find", entities=[], t_range=None, place=None, zone=None, scene_key=None, workspace="warehouse")
    q = models.Query(**base)
    assert q.scope == "scene"
    assert sc.schema_errors(q.model_dump(mode="json"), "search.json", "/$defs/query") == []
    assert models.Query(**{**base, "workspace": "street", "scope": "corpus"}).scope == "corpus"
    with pytest.raises(ValidationError):
        models.Query(**{**base, "scope": "everything"})


def test_corpus_asks_parse_into_models():
    asks = load("l1/asks_corpus.json")["asks"]
    assert any(a["q"] == ANCHOR for a in asks)
    rank = {"L3": 0, "L2": 1, "L1": 2}
    for ask in asks:
        assert ask["context"]["scope"] == "corpus"
        for f in ask["events"]:
            d = f["data"]
            if f["event"] == "tool_result":
                for e in (models.Evidence.model_validate(x) for x in d["evidence"]):
                    assert e.evidence_level == models.level_of(manifest(e.scene_key))
            elif f["event"] == "hits":
                cards = [models.MomentCard.model_validate(m) for m in d["moments"]]
                assert [rank[c.evidence_level] for c in cards] == sorted(rank[c.evidence_level] for c in cards)
                assert all(c.actor_refs == [] for c in cards if c.evidence_level == "L1")
            elif f["event"] == "claim":
                c = models.Claim.model_validate(d)
                if c.explain and "abstain" in c.explain:
                    assert c.explain["abstain"]["text"] == models.ABSTAIN_TEXT[c.explain["abstain"]["reason"]]
            elif f["event"] == "ui":
                assert "scene_key" in d


def test_index_adds_street_scene_only_on_request(tmp_path, index_db):
    import duckdb

    from fixtures.gen_fixture import build_index
    con = duckdb.connect(str(index_db), read_only=True)
    assert con.execute("SELECT count(*) FROM clips WHERE workspace = 'street'").fetchone() == (0,)
    con.close()
    build_index(tmp_path / "l1.duckdb", FIXTURES, l1=True)
    con = duckdb.connect(str(tmp_path / "l1.duckdb"), read_only=True)
    rows = con.execute("SELECT clip_id, n_person, n_vehicle, video_uri FROM clips WHERE workspace = 'street' ORDER BY 1").fetchall()
    assert rows == [(f"{ST_KEY}:fixture_cam-1:00000", 1, 1, "chunks/fixture_cam-1/0000.mp4"),
                    (f"{ST_KEY}:fixture_cam-1:00001", 1, 1, "chunks/fixture_cam-1/0000.mp4")]
    assert con.execute("SELECT workspace FROM scene_manifest WHERE scene_key = ?", [ST_KEY]).fetchone() == ("street",)


@pytest.mark.parametrize("key,tag", [(WH_KEY, "warehouse"), (FL_KEY, "fleet"), (ST_KEY, "fixed")])
def test_tag_of(key, tag):
    assert models.tag_of(manifest(key)) == tag


def test_street_cameras_are_fixed():
    st = manifest(ST_KEY)
    for camera in ("nyc_streets_cam-1", "nyc_streets_cam-2", "sf_streets_cam-4", "neighborhood_cam-1"):
        assert models.tag_of({**st, "sources": {"vss": {"camera_id": camera}}}) == "fixed"


def test_sources_are_tags():
    q = dict(template="find", entities=[], t_range=None, place=None, zone=None, scene_key=None, workspace="warehouse")
    assert models.Query(**q).sources is None
    assert models.Query(**q, sources=["warehouse", "fleet", "fixed"]).sources == ["warehouse", "fleet", "fixed"]
    with pytest.raises(ValidationError):
        models.Query(**q, sources=["street"])
