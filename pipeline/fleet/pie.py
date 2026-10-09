"""PIE inputs shared by the fleet scripts: trip ids and paths, OBD track and GPS states, camera calibration, OSM extract,
product header."""
from __future__ import annotations

import functools
import hashlib
import json
import math
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

from pipeline.common import paths

DATA = paths.DATA
PIE = DATA / "raw" / "pie"
OSM_JSON = DATA / "geo" / "pie_osm.json"
OSM_PBF = DATA / "raw" / "osm" / "ontario-latest.osm.pbf"
LANDMARK_TAGS = {"tourism": {"museum", "gallery"}, "amenity": {"university", "college"}, "leisure": {"stadium", "park"}}
ALIAS_KEYS = ("short_name", "alt_name", "official_name")
OUT = paths.OUT
CARD = "P-F"
CAM = "dashcam"
TRIPS_YAML = Path(__file__).resolve().parents[1] / "manifests" / "trips.yaml"
ANALYSIS_REV = "pf-1"
FPS = 30  # PIE videos; the OBD file has one row per video frame
# Fixes farther than MAX_OFF_DEG from central Toronto are glitches (a stray lat 11.3, a fix stuck 250 km east).
CENTRE, MAX_OFF_DEG = (43.67, -79.40), 0.2
M_PER_DEG = 111320.0  # metres per degree of latitude
MAX_GAP_MS = 2000  # CONTRACT §3 max_interpolation_gap_ms
GPS_SIGMA_M = 3.0  # horizontal error of a fresh fix of a consumer GPS; without a fix it grows by the distance driven
STOPPED_KMH = 1.0  # below this an unchanged fix is the car standing still, not a receiver without a new fix
HARD_BRAKE_MPS2 = -3.0  # about 0.3 g, the usual telematics harsh-braking threshold


def t_ms(frame) -> int:
    return round(frame * 1000 / FPS)


def video_ids() -> list[str]:
    return sorted(f"{p.parent.name}_{p.stem}" for p in PIE.glob("clips/set*/video_*.mp4"))


def scene_id(vid: str) -> str:
    return f"fl/pie/{vid}"


def scene_key(vid: str) -> str:
    return f"fl-pie-{vid}"


def vid_of(key: str) -> str:
    return key.removeprefix("fl-pie-")


def _split(vid: str) -> tuple[str, str]:
    s, _, n = vid.partition("_")
    return s, n


def clip(vid: str) -> Path:
    s, n = _split(vid)
    return PIE / "clips" / s / f"{n}.mp4"


def obd_path(vid: str) -> Path:
    s, n = _split(vid)
    return PIE / "annotations_vehicle" / s / f"{n}_obd.xml"


def annt_path(vid: str) -> Path:
    s, n = _split(vid)
    return PIE / "annotations" / s / f"{n}_annt.xml"


def attr_path(vid: str) -> Path:
    s, n = _split(vid)
    return PIE / "annotations_attributes" / s / f"{n}_attributes.xml"


def tmp_dir(key: str) -> Path:
    """Products of a scene, published as they are."""
    return OUT / "_tmp" / CARD / key


def work_dir(key: str) -> Path:
    """Intermediates of a scene (pose2d, map-match residuals); never published."""
    d = OUT / "_tmp" / CARD / "_work" / key
    d.mkdir(parents=True, exist_ok=True)
    return d


@functools.lru_cache(maxsize=4)
def obd(vid: str) -> dict[str, np.ndarray]:
    """Per video frame: lat, lon, heading (deg, true north clockwise), speed_kmh (OBD)."""
    rows = ET.parse(obd_path(vid)).getroot().findall("frame")
    get = lambda k: np.array([float(r.get(k)) for r in rows])  # noqa: E731
    return {"lat": get("latitude"), "lon": get("longitude"), "heading": get("heading_angle"), "speed_kmh": get("OBD_speed")}


def t1_ms(vid: str) -> int:
    return t_ms(len(obd(vid)["lat"]))


def gps_at(o: dict[str, np.ndarray], frames) -> list[dict]:
    """GPS state at each frame (CONTRACT §3). A fix is a valid position that changed since the previous frame, or any
    valid position while the car stands still. Between fixes at most MAX_GAP_MS apart the position is interpolated;
    in a longer gap it stays at the last fix (last_known); before the first fix it is the first fix (unknown).
    sigma_m = GPS_SIGMA_M + metres driven since (or until) the fix the position comes from."""
    lat, lon, spd = o["lat"], o["lon"], o["speed_kmh"]
    n = len(lat)
    valid = (np.abs(lat - CENTRE[0]) <= MAX_OFF_DEG) & (np.abs(lon - CENTRE[1]) <= MAX_OFF_DEG)
    moved = np.r_[True, (lat[1:] != lat[:-1]) | (lon[1:] != lon[:-1])]
    fix = valid & (moved | (spd < STOPPED_KMH))
    idx = np.arange(n)
    last = np.maximum.accumulate(np.where(fix, idx, -1))
    nxt = np.minimum.accumulate(np.where(fix, idx, n)[::-1])[::-1]
    driven = np.r_[0.0, np.cumsum(spd / 3.6 / FPS)]  # metres at the start of each frame
    out = []
    for f in frames:
        lf, nf = int(last[f]), int(nxt[f])
        if lf == f:
            state, src, la, lo, gap = "observed", f, lat[f], lon[f], 0
        elif lf < 0:
            state, src, la, lo, gap = "unknown", nf, lat[nf], lon[nf], 0
        elif nf < n and t_ms(nf) - t_ms(lf) <= MAX_GAP_MS:
            w = (f - lf) / (nf - lf)
            state, src, gap = "interpolated", f, t_ms(f) - t_ms(lf)
            la, lo = lat[lf] + w * (lat[nf] - lat[lf]), lon[lf] + w * (lon[nf] - lon[lf])
        else:
            state, src, la, lo, gap = "last_known", lf, lat[lf], lon[lf], t_ms(f) - t_ms(lf)
        out.append({"lat": float(la), "lon": float(lo), "heading": float(o["heading"][f]), "speed": float(spd[f] / 3.6),
                    "state": state, "gap_ms": int(gap), "sigma_m": GPS_SIGMA_M + abs(float(driven[f] - driven[src]))})
    return out


def accel(o: dict[str, np.ndarray]) -> np.ndarray:
    """Longitudinal acceleration (m/s^2) per frame from OBD speed: 0.5 s moving mean, then the change over 1 s; 0 where
    the windows do not fit inside the trip."""
    k = FPS // 2
    v = np.convolve(o["speed_kmh"] / 3.6, np.ones(k) / k, mode="valid")  # v[j]: mean of frames j .. j+k-1
    d = v[FPS:] - v[:-FPS]
    a = np.zeros(len(o["speed_kmh"]))
    c = (k - 1) // 2 + FPS // 2  # frame at the centre of d[0]
    a[c:c + len(d)] = d
    return a


@functools.lru_cache(maxsize=1)
def calibration() -> dict:
    return json.loads((PIE / "calibration_data.json").read_text())


def xy(lat, lon):
    """Metres east / north of CENTRE; the routes span ~0.1 deg, so one plane is good to ~0.2 %."""
    return (np.asarray(lon) - CENTRE[1]) * M_PER_DEG * math.cos(math.radians(CENTRE[0])), \
        (np.asarray(lat) - CENTRE[0]) * M_PER_DEG


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


@functools.lru_cache(maxsize=1)
def osm() -> dict:
    """D2's OSM extract (pie_osm.json) with plane coordinates: road segments, junctions (a node shared by two differently
    named roads), crossings, POIs, landmarks and building rings."""
    o = json.loads(OSM_JSON.read_text())
    pts = lambda xs: np.stack(xy([x["lat"] for x in xs], [x["lon"] for x in xs]), 1)  # noqa: E731
    for k in ("pois", "crossings", "landmarks"):
        o[k + "_xy"] = pts(o[k])
    a, b, owner, nodes = [], [], [], {}
    for i, r in enumerate(o["roads"]):
        for p, q in zip(r["coords"], r["coords"][1:]):
            a.append(p), b.append(q), owner.append(i)
        for p in r["coords"]:
            nodes.setdefault(tuple(p), set()).add(r["name"])
    o["road_a"], o["road_b"] = (np.stack(xy(*np.array(x).T), 1) for x in (a, b))
    o["road_of"] = np.array(owner)
    o["junctions"] = [(sorted(v), p) for p, v in nodes.items() if len(v) >= 2]
    o["junctions_xy"] = pts([{"lat": p[0], "lon": p[1]} for _, p in o["junctions"]])
    o["bld_rings"] = [np.stack(xy(*np.array(bd["ring"]).T), 1) for bd in o["buildings"]]
    o["bld_c"] = np.array([r.mean(0) for r in o["bld_rings"]])
    o["bld_r"] = np.array([np.hypot(*(r - r.mean(0)).T).max() for r in o["bld_rings"]])
    return o


def is_landmark(tags: dict) -> bool:
    """A named museum, gallery, university, college, stadium or park, or a named building with a wikidata id."""
    return "name" in tags and (any(tags.get(k) in v for k, v in LANDMARK_TAGS.items()) or ("building" in tags and "wikidata" in tags))


def osm_aliases(tags: dict) -> list[str]:
    out = [a.strip() for k in ALIAS_KEYS for a in tags.get(k, "").split(";") if a.strip()]
    return list(dict.fromkeys(a for a in out if a != tags["name"]))


def osm_landmarks(bbox: tuple[float, float, float, float]) -> list[dict]:
    """Landmarks (is_landmark) of the raw Ontario PBF whose geometry touches bbox = (lat0, lon0, lat1, lon1):
    [{id, name, aliases, geom}], geom GeoJSON Point (nodes) or MultiPolygon (closed ways, multipolygon relations).
    pie_osm.json lacks the alias tags and outlines, hence the PBF."""
    import osmium
    from shapely.geometry import box, shape
    area = box(bbox[1], bbox[0], bbox[3], bbox[2])
    gj = osmium.geom.GeoJSONFactory()
    fp = (osmium.FileProcessor(str(OSM_PBF)).with_locations()
          .with_areas(osmium.filter.KeyFilter(*LANDMARK_TAGS, "building")).with_filter(osmium.filter.KeyFilter("name")))
    out = []
    for o in fp:
        if not (o.is_node() or o.is_area()):
            continue
        tags = dict(o.tags)
        if not is_landmark(tags):
            continue
        try:
            geom = json.loads(gj.create_point(o) if o.is_node() else gj.create_multipolygon(o))
        except RuntimeError:  # an area with a broken ring
            continue
        if shape(geom).intersects(area):
            oid = f"n{o.id}" if o.is_node() else ("w" if o.from_way() else "r") + str(o.orig_id())
            out.append({"id": oid, "name": tags["name"], "aliases": osm_aliases(tags), "geom": geom})
    return out


@functools.lru_cache(maxsize=1)
def osm_building_geoms() -> dict[str, dict]:
    """GeoJSON MultiPolygon, holes included, of every building of the extract (pie_osm.json keeps only the outer rings),
    by extract id, read from the raw PBF."""
    import osmium
    want = {b["id"] for b in osm()["buildings"]}
    gj = osmium.geom.GeoJSONFactory()
    fp = (osmium.FileProcessor(str(OSM_PBF)).with_locations()
          .with_areas(osmium.filter.KeyFilter("building")).with_filter(osmium.filter.KeyFilter("building")))
    out = {}
    for o in fp:
        if o.is_area() and (oid := ("w" if o.from_way() else "r") + str(o.orig_id())) in want:
            try:
                out[oid] = json.loads(gj.create_multipolygon(o))
            except RuntimeError:  # an area with a broken ring
                continue
    return out


@functools.lru_cache(maxsize=64)
def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 22), b""):
            h.update(block)
    return "sha256:" + h.hexdigest()


def rel(p: Path) -> str:
    """Path relative to the data root (raw inputs) or to sva-out (products of other steps); a staged input
    _tmp/<card>/<scene>/... is named by the path publish.py moves it to, <scene>/..."""
    p = Path(p)
    for root in (DATA, OUT):
        if p.is_relative_to(root):
            parts = p.relative_to(root).parts
            return str(Path(*parts[2:])) if root == OUT and parts[0] == '_tmp' else str(Path(*parts))
    return str(p)


def header(inputs: list[Path]) -> dict:
    return {"schema_version": "1", "analysis_rev": ANALYSIS_REV,
            "producer_commit": os.environ.get("SVA_PRODUCER_COMMIT", "unknown"),
            "input_artifact_hashes": {rel(p): _sha256(str(p)) for p in inputs}}


def created() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write_json(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
    os.replace(tmp, path)
    return path


def load_boxes(scene: Path) -> list[list]:
    """All rows of boxes/<CAM>/*.json, time order."""
    rows = []
    for f in sorted((scene / "boxes" / CAM).glob("*.json")):
        rows += json.loads(f.read_text())["rows"]
    return rows


def entity_of(scene: Path) -> dict[int, tuple[str, str]]:
    """local_track_id -> (entity_id, kind) from entities.json."""
    ents = json.loads((scene / "entities.json").read_text())["entities"]
    return {o["local_track_id"]: (e["entity_id"], e["kind"]) for e in ents for o in e["observations"]}
