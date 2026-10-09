import json
import subprocess
import sys
from pathlib import Path

import pytest

SVA = Path(__file__).resolve().parents[3]
FIXTURES = SVA / "fixtures"
SVA_OUT = FIXTURES / "sva-out"
WH_KEY = "wh-sdg-fixture-01"
FL_KEY = "fl-pie-fixture-01"


def load(rel: str):
    return json.loads((FIXTURES / rel).read_text())


@pytest.fixture(scope="session")
def index_db(tmp_path_factory) -> Path:
    """index.duckdb built from the committed fixture (JSON only, no media)."""
    out = tmp_path_factory.mktemp("fx")
    db = out / "index.duckdb"
    subprocess.run([sys.executable, str(FIXTURES / "gen_fixture.py"), "--out", str(out), "--no-media", "--index", str(db)],
                   check=True, capture_output=True)
    return db
