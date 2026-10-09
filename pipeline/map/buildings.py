"""Building tiles: Toronto 3D Massing (2025 shapefile) as WGS84 polygons with one extrusion height, cut to the corridor,
tiled with two tippecanoe passes and joined into one PMTiles.

  python pipeline/map/buildings.py [--bbox W,S,E,N] [--out PATH]

The fixture (fixtures/sva-out/_map/buildings.pmtiles) is the same command with a small bbox.

Input SRC is 3dmassingshapefile_2025_wgs84.zip from https://open.toronto.ca/dataset/3d-massing/ (copied to ws; no
network here). Layers: `buildings` z13-16 every building, `massing` z10-12 only buildings >= TALL_M; properties
`bid` (m25-<shapefile record>, the dataset has no id) and `h` (m). Two passes because tippecanoe 2.49 writes near-empty
tiles when one pass uses per-feature minzoom.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import struct
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.polygon import orient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common import lease  # noqa: E402
from pipeline.map.basemap import BBOX, CARD, THREADS, TMP  # noqa: E402

SRC = TMP / "src" / "3dmassingshapefile_2025_wgs84.zip"
BASE = "3DMassingShapefile_2025_WGS84"
R = 6378137.0  # EPSG:3857 sphere radius: the shapefile is Web Mercator despite "WGS84" in its name
TALL_M = 25.0
MIN_AREA = 1.0  # m² in Web Mercator units; smaller rings and fragments are dropped
ATTRIBUTION = "Contains information licensed under the Open Government Licence – Toronto."


def height(max_h: float, avg_h: float) -> float:
    """MAX_HEIGHT is 0 for some of the tallest buildings, which carry their height in AVG_HEIGHT only."""
    return max_h if max_h > 0 else avg_h


def to_lonlat(xy: np.ndarray) -> np.ndarray:
    return np.column_stack([np.degrees(xy[:, 0] / R), np.degrees(np.arctan(np.sinh(xy[:, 1] / R)))])


def to_merc(lon: float, lat: float) -> tuple[float, float]:
    return R * math.radians(lon), R * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def _polygons(g):
    for p in shapely.get_parts(g):
        if p.geom_type == "Polygon":
            yield p
        elif p.geom_type in ("MultiPolygon", "GeometryCollection"):
            yield from _polygons(p)


def polygon(rings: list[list[tuple[float, float]]]) -> Polygon | MultiPolygon | None:
    """Shapefile rings (outers clockwise, holes counter-clockwise) -> valid polygon with holes kept, outers
    counter-clockwise (RFC 7946); None when nothing of at least MIN_AREA is left."""
    outers, holes = [], []
    for r in rings:
        if len(r) < 4 or Polygon(r).area < MIN_AREA:
            continue
        (holes if shapely.LinearRing(r).is_ccw else outers).append(shapely.make_valid(Polygon(r)))
    if not outers:
        return None
    g = shapely.union_all(outers)
    if holes:
        g = g.difference(shapely.union_all(holes))
    parts = [orient(p, 1.0) for p in _polygons(shapely.make_valid(g)) if p.area >= MIN_AREA]
    return None if not parts else parts[0] if len(parts) == 1 else MultiPolygon(parts)


def records(zf: zipfile.ZipFile):
    """Yields (record index, bbox in mercator, rings, MAX_HEIGHT, AVG_HEIGHT) of every non-null shape."""
    dbf = zf.read(BASE + ".dbf")
    hlen, rlen = struct.unpack("<HH", dbf[8:12])
    fields, i, pos = {}, 32, 1
    while dbf[i] != 0x0D:
        fields[dbf[i:i + 11].split(b"\x00")[0].decode()] = (pos, dbf[i + 16])
        pos += dbf[i + 16]
        i += 32

    def num(r, name):
        p, n = fields[name]
        return float(dbf[hlen + r * rlen + p:hlen + r * rlen + p + n].strip() or 0)

    shp = zf.read(BASE + ".shp")
    off, rec = 100, 0
    while off < len(shp):
        clen = struct.unpack(">i", shp[off + 4:off + 8])[0]
        body = shp[off + 8:off + 8 + 2 * clen]
        off += 8 + 2 * clen
        r, rec = rec, rec + 1
        if struct.unpack("<i", body[:4])[0] == 0:
            continue
        box = struct.unpack("<4d", body[4:36])
        nparts, npts = struct.unpack("<ii", body[36:44])
        starts = list(struct.unpack(f"<{nparts}i", body[44:44 + 4 * nparts])) + [npts]
        xy = np.frombuffer(body, "<f8", 2 * npts, 44 + 4 * nparts).reshape(-1, 2)
        yield r, box, [xy[a:b] for a, b in zip(starts, starts[1:])], num(r, "MAX_HEIGHT"), num(r, "AVG_HEIGHT")


def features(src: Path, bbox: list[float]):
    x0, y0 = to_merc(bbox[0], bbox[1])
    x1, y1 = to_merc(bbox[2], bbox[3])
    with zipfile.ZipFile(src) as zf:
        for r, (bx0, by0, bx1, by1), rings, max_h, avg_h in records(zf):
            if bx1 < x0 or bx0 > x1 or by1 < y0 or by0 > y1:
                continue
            g = polygon(rings)
            if g is None:
                continue
            g = shapely.clip_by_rect(shapely.transform(g, to_lonlat), *bbox)
            if g.is_empty:
                continue
            yield {"type": "Feature", "properties": {"bid": f"m25-{r}", "h": round(height(max_h, avg_h), 1)},
                   "geometry": json.loads(shapely.to_geojson(g))}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", default=BBOX)
    ap.add_argument("--out", type=Path, default=TMP / "_map" / "buildings.pmtiles")
    a = ap.parse_args()
    bbox = [float(v) for v in a.bbox.split(",")]
    work = TMP / "buildings"
    work.mkdir(parents=True, exist_ok=True)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    all_f, tall_f = work / "all.geojsons", work / "tall.geojsons"
    with lease.acquire(cpu=THREADS, owner=CARD):
        t = time.monotonic()
        n = n_tall = 0
        with all_f.open("w") as fa, tall_f.open("w") as ft:
            for f in features(SRC, bbox):
                line = json.dumps(f, separators=(",", ":")) + "\n"
                fa.write(line)
                n += 1
                if f["properties"]["h"] >= TALL_M:
                    ft.write(line)
                    n_tall += 1
        print(f"{n} buildings, {n_tall} >= {TALL_M:g} m, {time.monotonic() - t:.0f} s", flush=True)
        env = {**os.environ, "TIPPECANOE_MAX_THREADS": str(THREADS)}
        common = ["--no-feature-limit", "--no-tile-size-limit", "--attribution", ATTRIBUTION, "--force", "--quiet", "-P"]
        cmds = [
            ["tippecanoe", "-o", str(work / "all.pmtiles"), "-l", "buildings", "-Z13", "-z16", *common, str(all_f)],
            ["tippecanoe", "-o", str(work / "tall.pmtiles"), "-l", "massing", "-Z10", "-z12", *common, str(tall_f)],
            ["tile-join", "-f", "--quiet", "-o", str(a.out), "--attribution", ATTRIBUTION, str(work / "all.pmtiles"), str(work / "tall.pmtiles")],
        ]
        for cmd in cmds:
            print(" ".join(cmd), flush=True)
            subprocess.run(cmd, check=True, env=env)
    for f in (all_f, tall_f, work / "all.pmtiles", work / "tall.pmtiles"):
        f.unlink()
    print(f"{a.out}: {a.out.stat().st_size} B in {time.monotonic() - t:.0f} s")


if __name__ == "__main__":
    main()
