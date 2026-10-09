"""Places of every trip -> _tmp/P-F/<scene_key>/places.json, for geocoding and index_build.

  python pipeline/fleet/places.py

One row per place_id in the trip's trip.geojson: {place_id, kind junction|road, name, aliases[], lat, lon, geom}.
lat/lon = mean of the trip's GPS points with that place_id; geom = the junction point, or the matched road ways.
Plus kind landmark rows for the OSM landmarks (pie.osm_landmarks, read from the raw PBF) within LANDMARK_M of a GPS
point of the trip: aliases = OSM short_name / alt_name / official_name, lat/lon = centroid, geom = point or outline.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import yaml
from shapely import STRtree, transform
from shapely.geometry import MultiPoint, shape

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common import lease  # noqa: E402
from pipeline.fleet import pie  # noqa: E402

TYPES = {"Street": "St", "Avenue": "Ave", "Road": "Rd", "Drive": "Dr", "Boulevard": "Blvd", "Crescent": "Cres",
         "Place": "Pl", "Lane": "Ln", "Court": "Ct", "Terrace": "Terr"}
DIRS = {"West": "W", "East": "E", "North": "N", "South": "S"}
LANDMARK_M = 150.0


def _split(name: str) -> tuple[list[str], int]:
    """Words and the index of the street-type word (the last word, or the one before a trailing direction)."""
    w = name.split()
    return w, len(w) - 2 if len(w) >= 2 and w[-1] in DIRS else len(w) - 1


def short(name: str) -> str:
    """Only the trailing type and direction are abbreviated ("Avenue Road" -> "Avenue Rd")."""
    w, i = _split(name)
    if i >= 1:
        w[i] = TYPES.get(w[i], w[i])
        w[i + 1:] = [DIRS.get(x, x) for x in w[i + 1:]]
    return " ".join(w)


def road_aliases(name: str) -> list[str]:
    w, i = _split(name)
    out = {short(name)}
    if i >= 1 and w[i] in TYPES and not (i == 1 and w[0] in TYPES):
        out.add(" ".join(w[:i]))  # "Queen Street West" -> "Queen"
    return sorted(out - {name})


def junction_aliases(a: str, b: str) -> list[str]:
    out = set()
    for x, y in ((a, b), (b, a)):
        for p, q in ((x, y), (short(x), short(y))):
            out |= {f"{p} & {q}", f"{p} and {q}", f"{p} at {q}"}
    return sorted(out - {f"{a} & {b}"})


def trip_places(key: str, o: dict) -> list[dict]:
    pts = [f["properties"] for f in json.loads((pie.tmp_dir(key) / "trip.geojson").read_text())["features"][1:]]
    roads_by_name: dict[str, list[dict]] = {}
    for r in o["roads"]:
        roads_by_name.setdefault(r["name"], []).append(r)
    jn = {"pl_" + "_".join(pie.slug(n) for n in names): (names, p) for names, p in o["junctions"]}
    rows = []
    for pid in sorted({p["place_id"] for p in pts if p["place_id"]}):
        mine = [p for p in pts if p["place_id"] == pid]
        lat, lon = float(np.mean([p["lat"] for p in mine])), float(np.mean([p["lon"] for p in mine]))
        if pid in jn:
            names, (jlat, jlon) = jn[pid]
            rows.append({"place_id": pid, "kind": "junction", "name": " & ".join(names),
                         "aliases": junction_aliases(*names[:2]), "lat": round(lat, 7), "lon": round(lon, 7),
                         "geom": {"type": "Point", "coordinates": [jlon, jlat]}})
            continue
        ways = {p["way_id"] for p in mine}
        name = next(r["name"] for rs in roads_by_name.values() for r in rs if int(r["id"][1:]) in ways)
        lines = [[[c[1], c[0]] for c in r["coords"]] for r in roads_by_name[name] if int(r["id"][1:]) in ways]
        rows.append({"place_id": pid, "kind": "road", "name": name, "aliases": road_aliases(name),
                     "lat": round(lat, 7), "lon": round(lon, 7), "geom": {"type": "MultiLineString", "coordinates": lines}})
    return rows


def landmark_places(pts: list[dict], lms: list[dict]) -> list[dict]:
    """Rows for the landmarks of lms (pie.osm_landmarks) within LANDMARK_M of one of the GPS points pts."""
    if not pts or not lms:
        return []
    trip = MultiPoint(np.stack(pie.xy([p["lat"] for p in pts], [p["lon"] for p in pts]), 1))
    plane = [transform(shape(lm["geom"]), lambda c: np.stack(pie.xy(c[:, 1], c[:, 0]), 1)) for lm in lms]
    rows = []
    for i in sorted(STRtree(plane).query(trip, predicate="dwithin", distance=LANDMARK_M)):
        lm = lms[i]
        c = shape(lm["geom"]).centroid
        rows.append({"place_id": f"pl_lm_{lm['id']}", "kind": "landmark", "name": lm["name"], "aliases": lm["aliases"],
                     "lat": round(c.y, 7), "lon": round(c.x, 7), "geom": lm["geom"]})
    return rows


def main() -> None:
    trips = yaml.safe_load(pie.TRIPS_YAML.read_text())
    with lease.acquire(cpu=1, owner=pie.CARD):
        o = pie.osm()
        keys = [pie.scene_key(vid) for vid in [x["video"] for x in trips["full"]] + trips["gps_only"]]
        pts = {k: [f["properties"] for f in json.loads((pie.tmp_dir(k) / "trip.geojson").read_text())["features"][1:]] for k in keys}
        lat, lon = [p["lat"] for ps in pts.values() for p in ps], [p["lon"] for ps in pts.values() for p in ps]
        pad = 2 * LANDMARK_M / pie.M_PER_DEG  # degrees; doubled for longitude at 43.7 N
        lms = pie.osm_landmarks((min(lat) - pad, min(lon) - pad, max(lat) + pad, max(lon) + pad))
        print(f"osm landmarks in the trips' bbox: {len(lms)}", file=sys.stderr)
        for key in keys:
            rows = trip_places(key, o)
            lm_rows = landmark_places(pts[key], lms)
            pie.write_json(pie.tmp_dir(key) / "places.json",
                           {**pie.header([pie.OSM_JSON, pie.OSM_PBF, pie.tmp_dir(key) / "trip.geojson"]), "trip_id": key,
                            "places": rows + lm_rows})
            print(f"places {key}: {len(rows)} + {len(lm_rows)} landmarks", file=sys.stderr)


if __name__ == "__main__":
    main()
