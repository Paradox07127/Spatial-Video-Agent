"""F8: buildings.json for a trip: every building seen in the ego frames plus every building within ROUTE_M of the route.

  python pipeline/fleet/building_info.py SCENE_KEY

Height, name, address from the OSM extract (pie_osm.json); floors only where the height came from building:levels;
POIs and wikidata ids are the extract's POIs and landmarks inside the outline. Footprints from the raw PBF (the extract
keeps outer rings only), cleaned by footprint().
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from shapely import STRtree, get_parts, make_valid, orient_polygons, points
from shapely.geometry import MultiPolygon, mapping, shape

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common import lease  # noqa: E402
from pipeline.fleet import pie  # noqa: E402

ROUTE_M = 50.0
AVG_HEIGHT_M = 10.0  # a building with neither a height nor a levels tag
LEVEL_M = 3.3  # metres per storey the extract used to turn building:levels into a height


def footprint(g: dict) -> dict:
    """GeoJSON geometry g as a valid Polygon / MultiPolygon: overlapping parts merged, self-intersections split, holes
    kept, line and point fragments dropped; outer rings counterclockwise, holes clockwise (RFC 7946)."""
    parts = [p for p in get_parts(get_parts(make_valid(shape(g), method="structure"))) if p.geom_type == "Polygon"]
    return mapping(orient_polygons(parts[0] if len(parts) == 1 else MultiPolygon(parts)))


def run(key: str) -> Path:
    out = pie.tmp_dir(key)
    with lease.acquire(cpu=1, owner=pie.CARD):
        o = pie.osm()
        seen = {b["bid"] for f in sorted((out / "ego").glob("*.json")) for fr in json.loads(f.read_text())["frames"]
                for b in fr["buildings"]}
        trip = json.loads((out / "trip.geojson").read_text())
        route = np.stack(pie.xy(*np.array([[p["properties"]["lat"], p["properties"]["lon"]] for p in trip["features"][1:]]).T), 1)
        near, covered = set(), np.zeros(len(route), bool)
        for i, (c, r) in enumerate(zip(o["bld_c"], o["bld_r"])):
            # Outline vertices within ROUTE_M of a route point (bounding circle first).
            if (np.hypot(*(route - c).T) <= ROUTE_M + r).any():
                hit = (np.hypot(*(o["bld_rings"][i][:, None, :] - route[None]).transpose(2, 0, 1)) <= ROUTE_M).any(0)
                if hit.any():
                    near.add(o["buildings"][i]["id"])
                    covered |= hit
        bs = {b["id"]: b for b in o["buildings"] if b["id"] in seen | near}
        geoms = pie.osm_building_geoms()
        foot = {bid: footprint(geoms[bid]) for bid in bs}
        polys = {bid: shape(g) for bid, g in foot.items()}
        poi_tree = STRtree(points([[p["lon"], p["lat"]] for p in o["pois"]]))
        lm_tree = STRtree(points([[p["lon"], p["lat"]] for p in o["landmarks"]]))
        date = (o["source"].get("pbf_timestamp") or o["source"]["pbf_mtime"])[:10]
        out_b = {}
        for bid, b in bs.items():
            pois = [{"name": o["pois"][j]["name"], "kind": o["pois"][j]["kind"]}
                    for j in poi_tree.query(polys[bid], predicate="contains")]
            wiki = [o["landmarks"][j]["wikidata"] for j in lm_tree.query(polys[bid], predicate="contains")
                    if o["landmarks"][j]["wikidata"]]
            out_b[bid] = {"name": b["name"], "addr": b["addr"], "height_m": round(b["height_m"] or AVG_HEIGHT_M, 1),
                          "floors": round(b["height_m"] / LEVEL_M) if b["height_src"] == "levels" else None,
                          "pois": pois, "wikidata": wiki[0] if wiki else None,
                          "osm_id": ("way/" if bid[0] == "w" else "relation/") + bid[1:], "geom": foot[bid],
                          "source": "osm" if b["height_src"] else "avg_height", "map_snapshot_date": date}
        doc = {**pie.header([pie.OSM_JSON, pie.OSM_PBF, out / "trip.geojson"]), "map_snapshot_date": date, "buildings": out_b}
        pie.write_json(out / "buildings.json", doc)
    print(f"building_info {key}: {len(out_b)} buildings ({len(seen)} seen in frames, {len(near)} within {ROUTE_M:.0f} m of "
          f"the route), {(out / 'buildings.json').stat().st_size} B; route points with a building within {ROUTE_M:.0f} m: "
          f"{int(covered.sum())}/{len(route)}, all listed", file=sys.stderr)
    return out / "buildings.json"


if __name__ == "__main__":
    run(sys.argv[1])
