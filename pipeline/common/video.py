"""ffmpeg helpers shared by the pipeline cards.

CLI: python video.py transcode SRC DST | thumbs SRC OUT_DIR [--offset-ms N] | probe SRC
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

FF = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y"]


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode:
        raise RuntimeError(f"{cmd[0]} failed: {r.stderr.decode(errors='replace')[-400:]}")
    return r


def probe(src: Path) -> dict:
    """{w, h, fps, n_frames, duration_ms, codec} from ffprobe."""
    out = _run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries",
                "stream=width,height,r_frame_rate,nb_read_packets,codec_name:format=duration", "-of", "json", str(src)]).stdout
    d = json.loads(out)
    s = d["streams"][0]
    num, den = (int(x) for x in s["r_frame_rate"].split("/"))
    return {"w": s["width"], "h": s["height"], "fps": num / den, "n_frames": int(s["nb_read_packets"]),
            "duration_ms": round(float(d["format"]["duration"]) * 1000), "codec": s["codec_name"]}


def transcode(src: Path, dst: Path, *, gop_s: float = 1.0, crf: int = 23, max_w: int | None = None) -> Path:
    """H.264 yuv420p, keyframe every gop_s, +faststart (browser seek and Range playback). Threads capped at 4 per call."""
    fps = probe(src)["fps"]
    vf = ["-vf", f"scale='min({max_w},iw)':-2"] if max_w else []
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp.mp4")
    _run([*FF, "-threads", "4", "-i", str(src), *vf, "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
          "-pix_fmt", "yuv420p", "-g", str(max(1, round(fps * gop_s))), "-movflags", "+faststart", str(tmp)])
    tmp.replace(dst)
    return dst


def thumbs(src: Path, out_dir: Path, *, offset_ms: int = 0, width: int = 320) -> list[Path]:
    """1 fps JPEGs named <floor(t_scene_ms/1000):05d>.jpg where t_scene_ms = offset_ms + media_pts_ms; each is the first
    frame inside that scene second."""
    info = probe(src)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    s = max(offset_ms, 0) // 1000
    while (s * 1000 - offset_ms) < info["duration_ms"]:
        media_s = max(s * 1000 - offset_ms, 0) / 1000
        p = out_dir / f"{s:05d}.jpg"
        _run([*FF, "-ss", f"{media_s:.3f}", "-i", str(src), "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", str(p)])
        paths.append(p)
        s += 1
    return paths


def sprite(src: Path, dst: Path, *, every_s: float = 1.0, tile_w: int = 160, cols: int = 10) -> dict:
    """One JPEG grid; returns {tile_w, tile_h, cols, n, every_s}."""
    info = probe(src)
    n = max(1, math.floor(info["duration_ms"] / 1000 / every_s))
    rows = math.ceil(n / cols)
    dst.parent.mkdir(parents=True, exist_ok=True)
    _run([*FF, "-i", str(src), "-vf", f"fps=1/{every_s},scale={tile_w}:-2,tile={cols}x{rows}", "-frames:v", "1", "-q:v", "5", str(dst)])
    return {"tile_w": tile_w, "tile_h": round(info["h"] * tile_w / info["w"] / 2) * 2, "cols": cols, "n": n, "every_s": every_s}


def frames_at(src: Path | str, media_ms: list[int], *, width: int | None = None) -> list[bytes]:
    """JPEG bytes of the frame at each media time (src may be a local path or an http(s) URL)."""
    out = []
    for ms in media_ms:
        vf = ["-vf", f"scale={width}:-2"] if width else []
        r = _run([*FF, "-ss", f"{ms / 1000:.3f}", "-i", str(src), "-frames:v", "1", *vf, "-q:v", "3", "-f", "image2", "-c:v", "mjpeg", "pipe:1"])
        out.append(r.stdout)
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["transcode", "thumbs", "probe"])
    ap.add_argument("src")
    ap.add_argument("dst", nargs="?")
    ap.add_argument("--offset-ms", type=int, default=0)
    a = ap.parse_args()
    if a.cmd == "probe":
        print(json.dumps(probe(Path(a.src))))
    elif a.cmd == "transcode":
        print(transcode(Path(a.src), Path(a.dst)))
    else:
        print(len(thumbs(Path(a.src), Path(a.dst), offset_ms=a.offset_ms)), "thumbs", file=sys.stderr)
