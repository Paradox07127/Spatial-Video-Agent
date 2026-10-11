"""Validate scene products against schemas/*.json and check cross-file references.

  python pipeline/common/schema_check.py PATH [PATH ...]
  python pipeline/common/schema_check.py --file SCHEMA FILE   (one partial product, e.g. --file visibility.json x/visibility.json)
  python pipeline/common/schema_check.py --all [SVA_OUT]      (every published scene under SVA_OUT, default OUT_ROOT)

PATH is a scene directory (has manifest.json) or any directory above scenes (searched recursively). A fixtures
directory also gets asks.json and l1/asks_corpus.json (SSE frames + sequence rules), runs/*.json (agent_run) and
labels/*.json (situation_map).
A directory holding index.duckdb also gets: every scene_key cited by clips/events has a manifest under that directory.
Exit 0 when every check passes; prints one line per scene and the pass count.
"""
from __future__ import annotations

import base64
import json
import math
import struct
import sys
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common import paths  # noqa: E402

SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"
OUT_ROOT = paths.OUT
SCHEMA_BASE = "https://sva.local/schemas/"
POINTS_MAGIC = b"SVAP"
POINTS_HEADER = 32
SIZE_TARGETS = {"manifest.json": 20_000, "entities.json": 100_000, "timeline.json": 100_000, "events.json": 50_000,
                "visibility.json": 300_000, "forklift.json": 50_000, "tracks.json": 200_000, "trip.geojson": 300_000,
                "buildings.json": 2_000_000, "points.bin": 7_500_000}
SCENE_PREFIX = {"warehouse": "wh-", "fleet": "fl-", "street": "st-"}
ASK_FILES = ("asks.json", "l1/asks_corpus.json")


def _registry() -> tuple[Registry, dict[str, dict]]:
    docs = {p.name: json.loads(p.read_text()) for p in SCHEMAS.glob("*.json")}
    reg = Registry().with_resources((d["$id"], Resource.from_contents(d)) for d in docs.values())
    return reg, docs


REGISTRY, DOCS = _registry()
_VALIDATORS: dict[str, Draft202012Validator] = {}


def validator(name: str, pointer: str = "") -> Draft202012Validator:
    key = name + pointer
    if key not in _VALIDATORS:
        schema = {"$ref": SCHEMA_BASE + name + (("#" + pointer) if pointer else "")}
        _VALIDATORS[key] = Draft202012Validator(schema, registry=REGISTRY)
    return _VALIDATORS[key]


def schema_errors(obj, name: str, pointer: str = "", where: str = "") -> list[str]:
    out = []
    for e in sorted(validator(name, pointer).iter_errors(obj), key=lambda e: list(e.absolute_path)):
        path = "/".join(str(p) for p in e.absolute_path)
        out.append(f"{where}: {name}{pointer} at /{path}: {e.message[:300]}")
    return out


def scene_key_of(scene_id: str) -> str:
    return scene_id.replace("/", "-").lower()


def decode_visibility(vis: dict) -> list[bytes]:
    return [base64.b64decode(s) for s in vis["per_sec"]]


class SceneCheck:
    def __init__(self, root: Path):
        self.root = root
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.files = 0

    def err(self, msg: str) -> None:
        self.errors.append(f"{self.root.name}: {msg}")

    def load(self, rel: str, schema: str):
        p = self.root / rel
        if not p.is_file():
            self.err(f"missing {rel}")
            return None
        try:
            obj = json.loads(p.read_text())
        except json.JSONDecodeError as e:
            self.err(f"{rel}: invalid JSON: {e}")
            return None
        self.files += 1
        self.errors += schema_errors(obj, schema, where=f"{self.root.name}/{rel}")
        target = SIZE_TARGETS.get(Path(rel).name)
        if target and p.stat().st_size > target:
            self.warnings.append(f"{self.root.name}/{rel}: {p.stat().st_size} B > target {target} B")
        return obj

    def run(self) -> "SceneCheck":
        m = self.load("manifest.json", "manifest.json")
        if not m or self.errors:
            return self
        a, t0, t1 = m["assets"], m["t0_ms"], m["t1_ms"]
        cams = {c["id"]: c for c in m["cams"]}
        in_range = lambda t: t0 <= t <= t1  # noqa: E731
        if m["scene_key"] != scene_key_of(m["scene_id"]):
            self.err(f"scene_key {m['scene_key']} != scene_key_of({m['scene_id']})")
        if self.root.name != m["scene_key"]:
            self.err(f"directory name {self.root.name} != scene_key {m['scene_key']}")
        if not m["scene_key"].startswith(SCENE_PREFIX[m["workspace"]]):
            self.err(f"scene_key prefix of {m['scene_key']} does not match workspace {m['workspace']}")
        for c in m["cams"]:
            for k in ("video", "thumbs"):
                if c[k] and not (self.root / c[k]).exists():
                    self.err(f"cam {c['id']}: {k} {c[k]} missing")
            if c["thumbs"] and (self.root / c["thumbs"]).is_dir():
                n = len(list((self.root / c["thumbs"]).glob("*.jpg")))
                if n == 0:
                    self.err(f"cam {c['id']}: no thumbnails")
            if "chunks" in c:
                ends = [t0] + [x for ch in c["chunks"] for x in (ch["t0_ms"], ch["t1_ms"])] + [t1]
                if ends != sorted(ends) or any(ch["t0_ms"] >= ch["t1_ms"] for ch in c["chunks"]):
                    self.err(f"cam {c['id']}: chunks overlap, are unsorted, empty or outside the scene")
                for ch in c["chunks"]:
                    if not ch["uri"].startswith("s3://") and not (self.root / ch["uri"]).is_file():
                        self.err(f"cam {c['id']}: chunk {ch['uri']} missing")
                continue  # a 2D camera has no geometry to check
            if c["model"] == "fisheye" and len(c["dist"]) != 4:
                self.err(f"cam {c['id']}: fisheye needs 4 dist coefficients")
            fov = math.degrees(2 * math.atan(c["h"] / (2 * c["K"][1])))
            if abs(fov - c["fov_deg"]) > 0.01:
                self.err(f"cam {c['id']}: fov_deg {c['fov_deg']} != 2*atan(h/(2*fy)) = {fov:.4f}")

        ents = self.load(a["entities"], "entities.json") if "entities" in a else None
        ent_ids = {e["entity_id"] for e in ents["entities"]} if ents else None
        obs_owner: dict[tuple[str, int], str] = {}
        if ents:
            for e in ents["entities"]:
                for o in e["observations"]:
                    key = (o["cam_id"], o["local_track_id"])
                    if o["cam_id"] not in cams:
                        self.err(f"entities: {e['entity_id']} observation cam {o['cam_id']} not in manifest cams")
                    if key in obs_owner and obs_owner[key] != e["entity_id"]:
                        self.err(f"entities: observation {key} mapped to {obs_owner[key]} and {e['entity_id']}")
                    obs_owner[key] = e["entity_id"]

        def need_entity(eid: str, where: str) -> None:
            if ent_ids is not None and eid not in ent_ids:
                self.err(f"{where}: entity {eid} not in entities.json")

        events = self.load(a["events"], "events.json") if "events" in a else None
        event_ids = {e["id"] for e in events["events"]} if events else set()
        if events:
            for e in events["events"]:
                if not (in_range(e["t0_ms"]) and in_range(e["t1_ms"]) and e["t0_ms"] <= e["t1_ms"]):
                    self.err(f"events: {e['id']} time [{e['t0_ms']}, {e['t1_ms']}] outside scene or reversed")
                for x in e["actor_ids"]:
                    need_entity(x, f"events {e['id']}")

        if "timeline" in a:
            tl = self.load(a["timeline"], "timeline.json")
            if tl:
                for cam in tl["lanes"]:
                    if cam not in cams:
                        self.err(f"timeline: lane {cam} not in cams")
                for row in tl["sec"]:
                    for x in row[4]:
                        if x not in event_ids:
                            self.err(f"timeline: second {row[0]} cites unknown event {x}")

        ground = m.get("ground")
        if ground:
            for c in ground["cam_bit_order"]:
                if c not in cams:
                    self.err(f"ground.cam_bit_order: {c} not in cams")
        if "visibility" in a:
            vis = self.load(a["visibility"], "visibility.json")
            if vis and ground:
                for k in ("cell_m", "nx", "ny", "origin", "cam_bit_order"):
                    if vis[k] != ground[k]:
                        self.err(f"visibility.{k} {vis[k]} != manifest.ground.{k} {ground[k]}")
                n = vis["nx"] * vis["ny"] * len(vis["cam_bit_order"])
                secs = decode_visibility(vis)
                if len(secs) != math.ceil((t1 - t0) / 1000):
                    self.err(f"visibility: {len(secs)} seconds, scene has {math.ceil((t1 - t0) / 1000)}")
                for s, buf in enumerate(secs):
                    if len(buf) != n:
                        self.err(f"visibility: second {s} has {len(buf)} bytes, expected {n}")
                    elif max(buf) > 4:
                        self.err(f"visibility: second {s} has state code {max(buf)} > 4")

        if "skeleton" in a:
            sk = self.load(a["skeleton"], "skeleton.json")
            if sk:
                for pid, p in sk["persons"].items():
                    need_entity(pid, "skeleton")
                    n = len(p["t_ms"])
                    for k in ("root_world", "root_method", "root_sigma_m", "xyz", "conf", "n_views"):
                        if len(p[k]) != n:
                            self.err(f"skeleton {pid}: len({k}) {len(p[k])} != len(t_ms) {n}")
                    for i, t in enumerate(p["t_ms"]):
                        if not in_range(t):
                            self.err(f"skeleton {pid}: t_ms {t} outside scene")
                            break
                        if p["xyz"][i] is not None and max(p["n_views"][i]) < 2:
                            self.err(f"skeleton {pid}: frame {t} has joints from < 2 views")
                            break

        if "tracks" in a:
            tr = self.load(a["tracks"], "tracks.json")
            if tr:
                for x in tr["tracks"]:
                    need_entity(x["entity_id"], "tracks")
                    for c in x["cam_ids"]:
                        if c not in cams:
                            self.err(f"tracks {x['entity_id']}: cam {c} not in cams")
                    ts = [p[0] for p in x["pts"]]
                    if ts != sorted(ts) or (ts and not (in_range(ts[0]) and in_range(ts[-1]))):
                        self.err(f"tracks {x['entity_id']}: pts not sorted or outside scene")

        if "forklift" in a:
            fk = self.load(a["forklift"], "forklift.json")
            if fk:
                for f in fk["forklifts"]:
                    need_entity(f["entity_id"], "forklift")

        if "boxes_dir" in a:
            bdir = self.root / a["boxes_dir"]
            if not bdir.is_dir():
                self.err(f"boxes_dir {a['boxes_dir']} missing")
            else:
                for cdir in sorted(p for p in bdir.iterdir() if p.is_dir()):
                    if cdir.name not in cams:
                        self.err(f"boxes: dir {cdir.name} not in cams")
                    for f in sorted(cdir.glob("*.json")):
                        b = self.load(str(f.relative_to(self.root)), "boxes.json")
                        if not b:
                            continue
                        if b["cam_id"] != cdir.name or f.stem != f"{b['chunk_t0_ms'] // 10000:05d}":
                            self.err(f"boxes {f.relative_to(self.root)}: cam_id/chunk_t0_ms do not match the path")
                        lo, hi = b["chunk_t0_ms"], b["chunk_t0_ms"] + 10000
                        for r in b["rows"]:
                            if not lo <= r[0] < hi:
                                self.err(f"boxes {f.relative_to(self.root)}: t_ms {r[0]} outside chunk")
                                break
                            if ents is not None and (b["cam_id"], r[1]) not in obs_owner:
                                self.err(f"boxes {f.relative_to(self.root)}: track {r[1]} has no entity observation")
                                break

        if "dets_dir" in a:
            ddir = self.root / a["dets_dir"]
            if not ddir.is_dir():
                self.err(f"dets_dir {a['dets_dir']} missing")
            else:
                for cdir in sorted(p for p in ddir.iterdir() if p.is_dir()):
                    chunks = cams.get(cdir.name, {}).get("chunks")
                    if chunks is None:
                        self.err(f"dets: dir {cdir.name} is not a 2D camera")
                        continue
                    for f in sorted(cdir.glob("*.json")):
                        rel = f.relative_to(self.root)
                        d = self.load(str(rel), "dets2d.json")
                        if not d:
                            continue
                        i = d["chunk_index"]
                        if (d["cam_id"] != cdir.name or f.stem != f"{i:04d}" or i >= len(chunks)
                                or d["chunk_t0_ms"] != chunks[i]["t0_ms"]):
                            self.err(f"dets {rel}: cam_id/chunk_index/chunk_t0_ms do not match the path or the manifest chunks")
                            continue
                        lo, hi = chunks[i]["t0_ms"], chunks[i]["t1_ms"]
                        for r in d["rows"]:
                            if not lo <= r[0] < hi:
                                self.err(f"dets {rel}: t_ms {r[0]} outside chunk [{lo}, {hi})")
                                break

        if "cosmos" in a:
            co = self.load(a["cosmos"], "cosmos.json")
            if co:
                if co["scene_key"] != m["scene_key"]:
                    self.err("cosmos: scene_key mismatch")
                for r in co["records"]:
                    for ref in r["actor_refs"]:
                        need_entity(ref["entity_id"], f"cosmos {r['record_id']}")
                    for fr in r["input_frame_refs"]:
                        if fr["cam_id"] not in cams:
                            self.err(f"cosmos {r['record_id']}: frame cam {fr['cam_id']} not in cams")
                    if r["task"] == "C2":
                        mx = r["payload"]["max_frame_ms"]
                        frames = [fr["t_ms"] for fr in r["input_frame_refs"]]
                        if mx > r["t_ms"] or max(frames) != mx or min(frames) < r["t_ms"] - 2000 or max(frames) >= r["t_ms"]:
                            self.err(f"cosmos {r['record_id']}: C2 frames must lie in [t-2000, t) with max_frame_ms = max frame")

        if "points" in a:
            p = self.root / a["points"]
            if not p.is_file():
                self.err(f"missing {a['points']}")
            else:
                raw = p.read_bytes()
                self.files += 1
                if raw[:4] != POINTS_MAGIC:
                    self.err("points.bin: bad magic")
                else:
                    (n,) = struct.unpack_from("<I", raw, 4)
                    if len(raw) != POINTS_HEADER + 15 * n:
                        self.err(f"points.bin: {len(raw)} bytes, expected {POINTS_HEADER + 15 * n} for N={n}")
                    if m.get("points_meta", {}).get("n") != n:
                        self.err("points.bin: N differs from manifest.points_meta.n")

        trip = self.load(a["trip"], "trip.json") if "trip" in a else None
        if trip:
            if trip["features"][0]["properties"]["trip_id"] != trip["trip_id"]:
                self.err("trip: LineString trip_id mismatch")
            last = -1
            for f in trip["features"][1:]:
                pr = f["properties"]
                if pr["t_ms"] <= last or not in_range(pr["t_ms"]):
                    self.err(f"trip: gps t_ms {pr['t_ms']} not increasing or outside scene")
                    break
                last = pr["t_ms"]
                if pr["state"] == "interpolated" and pr["gap_ms"] > trip["max_interpolation_gap_ms"]:
                    self.err(f"trip: t_ms {pr['t_ms']} interpolated across a {pr['gap_ms']} ms gap")
                if pr["state"] == "observed" and pr["gap_ms"] != 0:
                    self.err(f"trip: t_ms {pr['t_ms']} observed with gap_ms {pr['gap_ms']}")
                if [round(pr["lon"], 7), round(pr["lat"], 7)] != [round(v, 7) for v in f["geometry"]["coordinates"]]:
                    self.err(f"trip: t_ms {pr['t_ms']} coordinates differ from lat/lon properties")

        bids = None
        if "buildings" in a:
            bl = self.load(a["buildings"], "buildings.json")
            bids = set(bl["buildings"]) if bl else None
        if "ego_dir" in a:
            edir = self.root / a["ego_dir"]
            for f in sorted(edir.glob("*.json")) if edir.is_dir() else []:
                eg = self.load(str(f.relative_to(self.root)), "ego.json")
                if not eg:
                    continue
                if f.stem != f"{eg['chunk_t0_ms'] // 10000:05d}":
                    self.err(f"ego {f.name}: chunk_t0_ms does not match the file name")
                for fr in eg["frames"]:
                    for x in fr["peds"] + fr["vehs"]:
                        need_entity(x["entity_id"], f"ego {fr['t_ms']}")
                    for b in fr["buildings"]:
                        if bids is not None and b["bid"] not in bids:
                            self.err(f"ego {fr['t_ms']}: building {b['bid']} not in buildings.json")
                            break
            if not edir.is_dir():
                self.err(f"ego_dir {a['ego_dir']} missing")
        return self


# ---------------------------------------------------------------- SSE sequence rules (§7.3)
ABSTAIN = {"UNSUPPORTED", "NO_MATCH", "OUT_OF_RANGE"}


def check_sse_sequence(frames: list[dict], *, protocol_version: int = 1) -> list[str]:
    """Schema of each frame plus ordering rules; returns error strings."""
    errs: list[str] = []
    for i, f in enumerate(frames):
        errs += schema_errors(f, "ask_sse_v2.json" if protocol_version == 2 else "ask_sse.json", where=f"frame {i}")
    if errs:
        return errs
    if not frames:
        return ["empty sequence"]
    run_ids = {f["data"]["run_id"] for f in frames}
    if len(run_ids) != 1:
        errs.append(f"frames carry {len(run_ids)} run_ids")
    if frames[0]["event"] != "step" or frames[0]["data"]["state"] != "parse":
        errs.append("first frame must be step(parse)")
    events = [f["event"] for f in frames]
    if events[-1] != "done":
        errs.append("last frame must be done")
    if events.count("status") != 1:
        errs.append("exactly one status frame")
    tail = events[events.index("status") + 1:] if "status" in events else []
    if tail not in (["done"], ["vss_native", "done"]) or events.count("vss_native") != tail.count("vss_native"):
        errs.append("status must be followed by done, or by one vss_native then done")
    steps = [f["data"]["i"] for f in frames if f["event"] == "step"]
    if steps != sorted(set(steps)):
        errs.append("step i must strictly increase")
    calls, read_eids, claims = set(), set(), set()
    evidence, observations = {}, {}
    final = next((f["data"]["final"] for f in frames if f["event"] == "status"), None)
    for f in frames:
        d = f["data"]
        if f["event"] == "tool_call":
            calls.add(d["call_id"])
        elif f["event"] == "tool_result":
            if d["call_id"] not in calls:
                errs.append(f"tool_result {d['call_id']} without tool_call")
            read_eids |= {e["eid"] for e in d["evidence"]}
            evidence.update({e['eid']: e for e in d['evidence']})
        elif f['event'] == 'observation':
            o = d['observation']
            observations[o['observation_id']] = o
            ev = evidence.get(o['eid'])
            if not ev or ev['kind'] != 'model' or any(ev[k] != o[k] for k in ('scene_key','cam_id','t0_ms','t1_ms')):
                errs.append('observation must cite a read model evidence with matching source and window')
            elif ev.get('value') != o:
                errs.append('observation content differs from the read model evidence')
            if not o['t0_ms'] < o['t1_ms'] or any(r['cam_id'] != o['cam_id'] or not o['t0_ms'] <= r['t_ms'] < o['t1_ms'] for r in o['frame_refs']):
                errs.append('observation frame is outside its input scope')
            for fact in o['facts']:
                if any(r not in o['frame_refs'] for r in fact['frame_refs']):
                    errs.append('observation fact cites an unread frame')
        elif f["event"] == "hits":
            if final in ABSTAIN and d["moments"]:
                errs.append(f"final {final} but hits carried moments")
            for mc in d["moments"]:
                if not set(mc["evidence_ids"]) <= read_eids:
                    errs.append(f"moment {mc['moment_id']} cites unread eids")
        elif f["event"] == "claim":
            claims.add(d["claim_id"])
            for p in d["predicates"]:
                if not set(p["eids"]) <= read_eids:
                    errs.append(f"claim {d['claim_id']} predicate {p['predicate_id']} cites unread eids")
        elif f["event"] == "answer":
            if not set(d["cites"]) <= read_eids or not {r["eid"] for r in d["measure_refs"]} <= read_eids:
                errs.append(f"answer {d['sentence_id']} cites eids outside read_eids")
            if not set(d["claim_ids"]) <= claims:
                errs.append(f"answer {d['sentence_id']} cites unknown claims")
            for ref in d.get('model_refs', []):
                o = observations.get(ref['observation_id'])
                index = int(ref['field'].split('.')[1])
                if not o or ref['eid'] != o['eid'] or index >= len(o['facts']) or ref['eid'] not in d['cites']:
                    errs.append('answer model_ref cites an unknown observation or fact')
                elif o['facts'][index]['text'] not in d['text']:
                    errs.append('answer model_ref text differs from the observed fact')
            for ref in d['measure_refs']:
                if protocol_version == 2 and evidence.get(ref['eid'], {}).get('method') == 'visual_observation':
                    errs.append('visual observation cannot be a measurement reference')
            last = 0
            for span in d.get('text_spans', []):
                if not last <= span['start'] < span['end'] <= len(d['text']) or span['eid'] not in read_eids:
                    errs.append('answer text span has invalid offsets or unread evidence')
                if not any(r['eid'] == span['eid'] and r['field'] == span['field'] for r in d['measure_refs']):
                    errs.append('answer text span must identify a measurement reference')
                last = span['end']
    return errs


def check_fixture_extras(root: Path) -> tuple[list[str], list[str]]:
    errs, notes = [], []
    for name in ASK_FILES:
        asks = root / name
        if not asks.is_file():
            continue
        doc = json.loads(asks.read_text())
        for ask in doc["asks"]:
            e = check_sse_sequence(ask["events"])
            final = ask["events"][-2]["data"].get("final")
            if final != ask["expect"]["final"]:
                e.append(f"expect.final {ask['expect']['final']} != status.final {final}")
            errs += [f"{name} {ask['ask_id']}: {x}" for x in e]
        notes.append(f"{name}: {len(doc['asks'])} asks")
    for p in sorted((root / "runs").glob("*.json")) if (root / "runs").is_dir() else []:
        errs += schema_errors(json.loads(p.read_text()), "agent_run.json", where=f"runs/{p.name}")
        notes.append(f"runs/{p.name}")
    for p in sorted((root / "labels").glob("*.json")) if (root / "labels").is_dir() else []:
        errs += schema_errors(json.loads(p.read_text()), "situation_map.json", where=f"labels/{p.name}")
        notes.append(f"labels/{p.name}")
    return errs, notes


def scene_dirs(path: Path) -> list[Path]:
    if (path / "manifest.json").is_file():
        return [path]
    return sorted(p.parent for p in path.rglob("manifest.json") if "_tmp" not in p.parts and not p.parent.name.startswith("."))


def check_index(db: Path, scene_keys: set[str]) -> list[str]:
    import duckdb
    con = duckdb.connect(str(db), read_only=True)
    errs = [f"{db.name}: {t} cites scene_key {k} without a manifest"
            for t in ("clips", "events")
            for (k,) in con.execute(f"SELECT DISTINCT scene_key FROM {t} ORDER BY 1").fetchall() if k not in scene_keys]
    con.close()
    return errs


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    if argv[0] == "--file" and len(argv) == 3:
        errs = schema_errors(json.loads(Path(argv[2]).read_text()), argv[1], where=argv[2])
        print("\n".join(errs) or f"OK   {argv[2]} ({argv[1]})")
        return 1 if errs else 0
    if argv[0] == "--all":
        argv = argv[1:] or [str(OUT_ROOT)]
    n_err, n_scenes, n_ok = 0, 0, 0
    for arg in argv:
        root = Path(arg)
        scenes = scene_dirs(root)
        if not scenes and not (root / "asks.json").is_file():
            print(f"FAIL {root}: no scenes found")
            n_err += 1
        for s in scenes:
            c = SceneCheck(s).run()
            n_scenes += 1
            for w in c.warnings:
                print(f"  WARN {w}")
            if c.errors:
                n_err += len(c.errors)
                print(f"FAIL {s.name} ({len(c.errors)} errors)")
                for e in c.errors[:50]:
                    print(f"  {e}")
            else:
                n_ok += 1
                print(f"OK   {s.name} ({c.files} files)")
        if (root / "index.duckdb").is_file():
            errs = check_index(root / "index.duckdb", {s.name for s in scenes})
            for e in errs[:50]:
                print(f"  {e}")
            print(("FAIL" if errs else "OK  ") + f" {root / 'index.duckdb'} scene_keys")
            n_err += len(errs)
        if root.is_dir():
            errs, notes = check_fixture_extras(root)
            for e in errs:
                print(f"  {e}")
            if notes:
                print(("FAIL" if errs else "OK  ") + f" {root} extras: " + ", ".join(notes))
            n_err += len(errs)
    print(f"schema_check: {'PASS' if n_err == 0 else 'FAIL'} ({n_err} errors; {n_ok}/{n_scenes} scenes pass)")
    return 1 if n_err else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
