"""Assemble and publish warehouse scenes: SDG runs (P-W1 products + this card's) and SmartSpaces Warehouse_017.

  python pipeline/warehouse/run_all.py RUN | warehouse_017 | --all

SDG run: pose2d (skipped when its pose2d/*.json exist), triangulate, forklift, events. When P-W1 has written
_tmp/P-W1/<scene_key>/manifest.part.json, its points/visibility/video/thumbs are copied in, then entities, tracks, boxes,
timeline and manifest are written into _tmp/P-W2/<scene_key>/ and the scene is published. A run without P-W1 products is
recorded as skipped. warehouse_017: smartspaces.py products, same assembly. Every outcome goes to
pipeline/manifests/scenes.yaml.
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import shutil
import sys
import traceback
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common import lease, publish  # noqa: E402
from pipeline.warehouse import events, forklift, pose2d, sdg, smartspaces, triangulate  # noqa: E402
from pipeline.warehouse.pose2d import OUT_ROOT, OWNER, WORK, header, write_json  # noqa: E402

PW1 = OUT_ROOT / "_tmp" / "P-W1"
PW1_FILES = ["points.bin", "visibility.json", "video", "thumbs"]
SCENES_YAML = Path(__file__).resolve().parents[1] / "manifests" / "scenes.yaml"
SS = "warehouse_017"


def _lanes(cover: set[int], n_s: int, t1: int) -> list[list]:
    out: list[list] = []
    for s in range(n_s):
        st = "ok" if s in cover else "no_video"
        if out and out[-1][2] == st:
            out[-1][1] = min((s + 1) * 1000, t1)
        else:
            out.append([s * 1000, min((s + 1) * 1000, t1), st])
    return out


def timeline(t1: int, cams: list[str], poses: dict, persons: dict, fks: list[dict], evs: list[dict]) -> dict:
    """Coverage of a camera second = pose2d has a frame of that camera in the second (frames are decoded video)."""
    n_s = math.ceil(t1 / 1000)
    cover = {c: {t // 1000 for t, _ in poses[c]["frames"]} for c in cams}
    p_sec = [{t // 1000 for t in p["t_ms"]} for p in persons.values()]
    f_sec = [{q["t_ms"] // 1000 for q in f["track"]} for f in fks]
    sec = [[s, sum(s in x for x in p_sec), sum(s in x for x in f_sec), sum(1 << i for i, c in enumerate(cams) if s in cover[c]),
            [e["id"] for e in evs if e["t0_ms"] < (s + 1) * 1000 and e["t1_ms"] > s * 1000]] for s in range(n_s)]
    return {"sec": sec, "lanes": {c: _lanes(cover[c], n_s, t1) for c in cams}}


def assemble(key: str, base: dict, base_path: Path) -> None:
    """Writes entities, tracks, boxes, timeline and manifest from the products in the scene and work dirs."""
    out, wd = OUT_ROOT / "_tmp" / OWNER / key, WORK / key
    cams = [c["id"] for c in base["cams"]]
    t1 = base["t1_ms"]
    poses = pose2d.load(key, cams)
    body = json.loads((wd / "persons.json").read_text())
    persons = json.loads((out / "skeleton.json").read_text())["persons"]
    evs = json.loads((out / "events.json").read_text())["events"]
    fks = json.loads((out / "forklift.json").read_text())["forklifts"] if (out / "forklift.json").is_file() else []
    ents, trs, rows = body["entities"], body["tracks"], body["boxes"]
    step = round(1000 / poses[cams[0]]["fps"])
    if fks:
        fb = json.loads((wd / "forklift_obs.json").read_text())["boxes"]
        for f in fks:
            ts = [q["t_ms"] for q in f["track"]]
            ents.append({"entity_id": f["entity_id"], "kind": "forklift",
                         "observations": [{"cam_id": c, "local_track_id": forklift.GT_TRACK_ID, "t0_ms": r[0][0],
                                           "t1_ms": min(r[-1][0] + step, t1), "method": "gt", "conf": 1.0} for c, r in fb.items() if r],
                         "association": {"method": "gt", "conf": 1.0, "valid": [ts[0], min(ts[-1] + step, t1)]}})
            trs.append({"entity_id": f["entity_id"], "cls": "forklift", "t0_ms": ts[0], "t1_ms": min(ts[-1] + step, t1),
                        "cam_ids": [c for c, r in fb.items() if r], "source": f["source"],
                        "pts": [[q["t_ms"], q["x"], q["y"], q["sigma_m"], "observed"] for q in f["track"]]})
            for c, r in fb.items():
                rows[c] += r
    inputs = [wd / "persons.json", out / "skeleton.json", out / "events.json"] + ([wd / "forklift_obs.json", out / "forklift.json"] if fks else [])
    write_json(out / "entities.json", {**header(inputs), "entities": ents})
    write_json(out / "tracks.json", {**header(inputs), "tracks": trs})
    shutil.rmtree(out / "boxes", ignore_errors=True)
    for c, rs in rows.items():
        chunks: dict[int, list] = {}
        for r in sorted(rs, key=lambda r: (r[0], r[1])):
            chunks.setdefault(r[0] // 10000, []).append(r)
        for k, cr in chunks.items():
            write_json(out / "boxes" / c / f"{k:05d}.json", {**header(inputs), "cam_id": c, "chunk_t0_ms": k * 10000, "rows": cr})
    write_json(out / "timeline.json", {**header(inputs), **timeline(t1, cams, poses, persons, fks, evs)})
    assets = {"entities": "entities.json", "timeline": "timeline.json", "events": "events.json", "tracks": "tracks.json",
              "boxes_dir": "boxes", "skeleton": "skeleton.json"}
    for k, f in (("forklift", "forklift.json"), ("points", "points.bin"), ("visibility", "visibility.json")):
        if (out / f).is_file():
            assets[k] = f
    models = dict(base["models"])
    prev = OUT_ROOT / key / "manifest.json"
    if prev.is_file():  # keep what other cards patched into the published manifest (cosmos records)
        pm = json.loads(prev.read_text())
        assets = {**{k: v for k, v in pm["assets"].items() if k not in assets}, **assets}
        models = {**pm["pipeline"]["models"], **models}
    m = {**header([base_path, out / "entities.json", out / "tracks.json", out / "timeline.json"]), "scene_id": base["scene_id"],
         "scene_key": key, "workspace": "warehouse", "title": base["title"], "t0_ms": 0, "t1_ms": t1, "fps": base["fps"],
         "world_frame_id": base["world_frame_id"], "metric": base["metric"], "cams": base["cams"], "ground": base["ground"],
         **({"points_meta": base["points_meta"]} if "points_meta" in base else {}), "assets": assets, "sources": base["sources"],
         "pipeline": {"models": models, "created": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}}
    write_json(out / "manifest.json", m)


def sdg_scene(rd: Path) -> dict:
    key = pose2d.scene_key(rd)
    wd = pose2d.work_dir(key)
    if not all((wd / "pose2d" / f"{c}.json").is_file() for c in sdg.DISPLAY_CAMS):
        with lease.acquire(cpu=4, gpu=1, owner=OWNER):
            pose2d.run_pose({c: rd / f"{c}.rgb.mp4" for c in sdg.DISPLAY_CAMS}, 15, wd)
    with lease.acquire(cpu=2, owner=OWNER):
        persons = triangulate.run(rd)
        fo = forklift.run(rd)
        evs = events.run(rd)
        row = {"scene_id": sdg.scene_id(rd), "source": rd.name, "n_persons": len(persons), "n_events": len(evs),
               "max_severity": max((e["severity"] for e in evs), default=0), "forklift_source": "gt",
               "yolo_vehicle_recall": fo["recall"]}
        part_path = PW1 / key / "manifest.part.json"
        if not part_path.is_file():
            return {**row, "status": "skipped", "note": "no P-W1 manifest.part.json yet"}
        out = OUT_ROOT / "_tmp" / OWNER / key
        for name in PW1_FILES:
            src = PW1 / key / name
            if src.is_dir():
                shutil.copytree(src, out / name, dirs_exist_ok=True)
            elif src.is_file():
                shutil.copy2(src, out / name)
        part = json.loads(part_path.read_text())
        m = sdg.RUN_RE.match(rd.name)
        base = {"scene_id": part["scene_id"], "title": f"SDG {m.group(1)[:8]} {m.group(2)}", "t1_ms": 10000, "fps": 30,
                "world_frame_id": part["world_frame_id"], "metric": True, "cams": part["cams"], "ground": part["ground"],
                "points_meta": part["points_meta"],
                "sources": {**part["sources"], "sdg_run": rd.name, "pw1": {"analysis_rev": part["analysis_rev"],
                                                                           "producer_commit": part["producer_commit"]},
                            "forklift": {"source": "gt", "yolo_vehicle_recall": fo["recall"], "n_gt_boxes": fo["n_gt"]}},
                "models": {**pose2d.MODELS, "forklift": "sdg_gt_bbox3d"}}
        assemble(key, base, part_path)
        publish.publish(OWNER, key)
    return {**row, "status": "published", "note": ""}


def ss_scene() -> dict:
    base = smartspaces.run()
    assemble(smartspaces.KEY, base, WORK / smartspaces.KEY / "manifest.base.json")
    with lease.acquire(cpu=2, owner=OWNER):
        publish.publish(OWNER, smartspaces.KEY)
    persons = json.loads((OUT_ROOT / smartspaces.KEY / "skeleton.json").read_text())["persons"]
    evs = json.loads((OUT_ROOT / smartspaces.KEY / "events.json").read_text())["events"]
    return {"scene_id": smartspaces.SCENE_ID, "source": str(smartspaces.SS_DIR), "n_persons": len(persons), "n_events": len(evs),
            "max_severity": max((e["severity"] for e in evs), default=0), "forklift_source": "none", "status": "published",
            "note": ""}


def record(key: str, row: dict) -> None:
    doc = yaml.safe_load(SCENES_YAML.read_text()) if SCENES_YAML.is_file() else None
    rows = {r["scene_key"]: r for r in (doc or {}).get("scenes", [])}
    rows[key] = {"scene_key": key, **row, "updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}
    SCENES_YAML.write_text("# Written by pipeline/warehouse/run_all.py: every warehouse scene and its last outcome.\n"
                           + yaml.safe_dump({"scenes": sorted(rows.values(), key=lambda r: r["scene_key"])}, sort_keys=False))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run", nargs="?", help=f"SDG run dir name or {SS}")
    ap.add_argument("--all", action="store_true", help=f"all SDG runs, then {SS}")
    a = ap.parse_args()
    targets = sorted(p.name for p in sdg.SDG_ROOT.iterdir() if sdg.RUN_RE.match(p.name)) + [SS] if a.all else [a.run]
    for t in targets:
        key = smartspaces.KEY if t == SS else pose2d.scene_key(pose2d.run_dir(t))
        try:
            row = ss_scene() if t == SS else sdg_scene(pose2d.run_dir(t))
        except (Exception, SystemExit) as e:  # one bad scene must not stop the batch; the error is recorded
            traceback.print_exc()
            row = {"source": t, "status": "failed", "note": str(e)[:300]}
        record(key, row)
        print(f"run_all {key}: {row['status']} {row.get('note', '')}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
