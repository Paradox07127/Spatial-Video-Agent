"""Offline basemap: OpenMapTiles-schema vector tiles of the PIE corridor, cut by planetiler from the Ontario extract.

  python pipeline/map/basemap.py [--bbox W,S,E,N] [--minzoom Z] [--maxzoom Z] [--exclude-layers L,..] [--out PATH]

The fixture (fixtures/sva-out/_map/basemap.pmtiles) is the same command with a small bbox, --minzoom 13 --maxzoom 13
and --exclude-layers housenumber,poi,building.

No network: the three auxiliary sources (water polygons, Natural Earth, lake centerlines) are read from SOURCES, which
Urban-Dossier's planetiler run downloaded; planetiler itself is the pinned JAR (v0.10.2 release asset).
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common import lease, paths  # noqa: E402

CARD = "M1"
OUT = paths.OUT
TMP = OUT / "_tmp" / CARD
PBF = paths.DATA / "raw" / "osm" / "ontario-latest.osm.pbf"
SOURCES = Path("/mnt/data/urban-dossier-state/maps/sources")
JAR = TMP / "tools" / "planetiler-0.10.2.jar"
JAR_SHA256 = "f310bd0413e2e4512b27f4046d418664e8e1d3bf31603c2a70e23de06c167e4d"
# The 53 PIE trips' extent padded by about 0.5 km.
BBOX = "-79.4931,43.6288,-79.3487,43.7493"
THREADS = 8


def command(bbox: str, minzoom: int, maxzoom: int, exclude: str, out: Path, work: Path) -> list[str]:
    return [
        # ActiveProcessorCount keeps the JVM's GC and common pool inside the lease.
        "java", "-Xmx8g", f"-XX:ActiveProcessorCount={THREADS}", "-jar", str(JAR),
        f"--osm_path={PBF}", f"--bounds={bbox}", f"--output={out}", f"--tmpdir={work}",
        f"--water_polygons_path={SOURCES / 'water-polygons-split-3857.zip'}",
        f"--natural_earth_path={SOURCES / 'natural_earth_vector.sqlite.zip'}",
        f"--lake_centerlines_path={SOURCES / 'lake_centerline.shp.zip'}",
        "--languages=en", f"--exclude-layers={exclude}", f"--minzoom={minzoom}", f"--maxzoom={maxzoom}", f"--render-maxzoom={maxzoom}",
        "--use-wikidata=false", f"--threads={THREADS}", "--force",
    ]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", default=BBOX)
    ap.add_argument("--minzoom", type=int, default=0)
    ap.add_argument("--maxzoom", type=int, default=14)
    ap.add_argument("--exclude-layers", default="housenumber")
    ap.add_argument("--out", type=Path, default=TMP / "_map" / "basemap.pmtiles")
    a = ap.parse_args()
    if hashlib.sha256(JAR.read_bytes()).hexdigest() != JAR_SHA256:
        sys.exit(f"{JAR}: sha256 mismatch")
    work = TMP / "planetiler"
    a.out.parent.mkdir(parents=True, exist_ok=True)
    cmd = command(a.bbox, a.minzoom, a.maxzoom, a.exclude_layers, a.out, work)
    print(" ".join(cmd), flush=True)
    with lease.acquire(cpu=THREADS, owner=CARD):
        t = time.monotonic()
        subprocess.run(cmd, check=True)
    print(f"{a.out}: {a.out.stat().st_size} B in {time.monotonic() - t:.0f} s")


if __name__ == "__main__":
    main()
