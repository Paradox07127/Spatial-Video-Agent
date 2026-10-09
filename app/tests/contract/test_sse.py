"""asks.json frames match ask_sse.json and the §7.3 ordering rules; the rules reject the failure modes they target."""
import copy
import json

import pytest

from pipeline.common.schema_check import check_sse_sequence

from .conftest import SVA, load

ASKS = {a["ask_id"]: a for a in load("asks.json")["asks"]}


def test_ask_set():
    assert set(ASKS) == {"wh-main", "wh-refuse", "wh-nomatch", "fl-where"}
    assert {a["expect"]["final"] for a in ASKS.values()} == {"SUPPORTED", "UNSUPPORTED", "NO_MATCH"}


@pytest.mark.parametrize("ask_id", sorted(ASKS))
def test_sequences_valid(ask_id):
    assert check_sse_sequence(ASKS[ask_id]["events"]) == []


def test_main_ask_flips_claim():
    claims = [f["data"] for f in ASKS["wh-main"]["events"] if f["event"] == "claim"]
    assert [(c["before"], c["after"]) for c in claims] == [(None, "UNRESOLVED"), ("UNRESOLVED", "SUPPORTED")]
    hits = [f for f in ASKS["wh-main"]["events"] if f["event"] == "hits"]
    assert len(hits) == 1 and hits[0]["data"]["moments"][0]["cams"] == ["ceiling_00", "ceiling_04", "eye_00"]


def _mut(ask_id, fn):
    frames = copy.deepcopy(ASKS[ask_id]["events"])
    fn(frames)
    return check_sse_sequence(frames)


def test_rules_reject():
    unread = "e_" + "0" * 16
    assert _mut("wh-main", lambda f: next(x for x in f if x["event"] == "answer")["data"]["cites"].append(unread))
    assert _mut("wh-main", lambda f: f.pop())  # no done
    assert _mut("wh-main", lambda f: f.insert(0, f.pop(1)))  # first frame not parse
    assert _mut("wh-main", lambda f: f[3]["data"].__setitem__("run_id", "r_otherrun00001"))

    def abstain_with_hits(f):
        h = next(x for x in f if x["event"] == "hits")
        h["data"]["moments"].append(copy.deepcopy(next(x for x in ASKS["wh-main"]["events"] if x["event"] == "hits")["data"]["moments"][0]))
    assert _mut("wh-nomatch", abstain_with_hits)
    assert _mut("wh-main", lambda f: next(x for x in f if x["event"] == "ui")["data"].__setitem__("cmd", "explode"))


def test_vss_native_only_between_status_and_done():
    run_id = ASKS["wh-main"]["events"][0]["data"]["run_id"]
    native = {"event": "vss_native", "data": {"run_id": run_id, "status": "timeout", "elapsed_ms": 10000, "sql_query": None, "hits": []}}
    assert "status, then vss_native" in json.loads((SVA / "schemas" / "ask_sse.json").read_text())["title"]
    assert _mut("wh-main", lambda f: f.insert(-1, copy.deepcopy(native))) == []
    assert _mut("wh-main", lambda f: f.insert(-2, copy.deepcopy(native)))  # before status
    assert _mut("wh-main", lambda f: f.append(copy.deepcopy(native)))  # after done

    def twice(f):
        f[-1:-1] = copy.deepcopy([native, native])
    assert _mut("wh-main", twice)
