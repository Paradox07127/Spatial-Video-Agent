"""Data root of the offline pipeline; SVA_ROOT moves it to another filesystem."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.environ.get("SVA_ROOT", "/mnt/data/vast-hack"))
DATA = ROOT / "data"
OUT = ROOT / "sva-out"
