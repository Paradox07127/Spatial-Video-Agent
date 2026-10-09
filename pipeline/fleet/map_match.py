"""F6: map-match a GPS point to the OSM extract: nearest named road segment with heading preference (D2 geo.py:192-198),
nearest junction, the road's maxspeed and a place id. The residual is the distance to the matched segment."""
from __future__ import annotations

import numpy as np

from pipeline.fleet import pie

ROAD_M = 30.0  # farther than this from every named road: no way_id
ALONG_DEG = 30.0  # a segment within this of the heading (either way) wins when it is within ROAD_M
JUNCTION_M = 25.0  # within this of a junction the place is the junction
MPH_KMH = 1.609344


def _seg_dist(p, a, b):
    """Distance from p to each segment a[i]-b[i] ((n, 2) arrays, metres)."""
    ab = b - a
    t = np.clip(((p - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-9), 0, 1)
    return np.hypot(*(a + t[:, None] * ab - p).T)


def _rel(d, heading):
    """Bearing of offsets d (n, 2) relative to the heading, degrees in [-180, 180)."""
    return (np.degrees(np.arctan2(d[:, 0], d[:, 1])) - heading + 540) % 360 - 180


def maxspeed_kmh(tag: str | None) -> float | None:
    """OSM maxspeed: plain km/h or "<n> mph"; anything else (none, signals, walk) is None."""
    if not tag:
        return None
    num, *unit = tag.split()
    if unit not in ([], ["mph"]) or not num.replace(".", "", 1).isdigit():
        return None
    return round(float(num) * (MPH_KMH if unit else 1.0), 1)


def match(o: dict, lat: float, lon: float, heading: float) -> dict:
    """{way_id, node_id, place_id, maxspeed_kmh, residual_m, road, junction, junction_m} for one GPS point."""
    p0 = np.array(pie.xy(lat, lon), float)
    rd = _seg_dist(p0, o["road_a"], o["road_b"])
    along = (np.abs((_rel(o["road_b"] - o["road_a"], heading) + 90) % 180 - 90) <= ALONG_DEG) & (rd <= ROAD_M)
    k = int(np.where(along, rd, np.inf).argmin()) if along.any() else int(rd.argmin())
    road = o["roads"][o["road_of"][k]] if rd[k] <= ROAD_M else None
    jd = np.hypot(*(o["junctions_xy"] - p0).T)
    j = int(jd.argmin())
    names = o["junctions"][j][0] if jd[j] <= JUNCTION_M else None
    node = None
    if names:
        # The extract keeps no node ids on roads: the junction's node is the nearest signal or crossing node.
        cd = np.hypot(*(o["crossings_xy"] - o["junctions_xy"][j]).T)
        c = int(cd.argmin())
        node = int(o["crossings"][c]["id"][1:]) if cd[c] <= JUNCTION_M else None
    place = ("pl_" + "_".join(pie.slug(n) for n in names)) if names else ("pl_" + pie.slug(road["name"])) if road else None
    return {"way_id": int(road["id"][1:]) if road else None, "node_id": node, "place_id": place,
            "maxspeed_kmh": maxspeed_kmh(road["maxspeed"]) if road else None,
            "residual_m": round(float(rd[k]), 2), "road": road["name"] if road else None, "junction": names,
            "junction_m": round(float(jd[j]), 1)}
