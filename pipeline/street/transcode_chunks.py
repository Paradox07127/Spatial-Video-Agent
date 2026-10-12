"""Re-encode local street chunks that are not H.264 (VSS keeps HEVC Main 10 sources, which most browsers cannot decode).

  python pipeline/street/transcode_chunks.py --backup DIR [--sva-out DIR] [--encoder libx264|h264_nvenc] [SCENE_KEY ...]

Each relative chunk uri of a street manifest is replaced in place by H.264 yuv420p, keyframe every second, +faststart; the
audio is copied, so the video keeps its start offset against the audio and the scene times of dets and captions still hold.
The original moves to <backup>/<scene_key>/<uri>. s3:// chunks and chunks already in H.264 are left alone.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common import paths, video  # noqa: E402


def codec(path: Path) -> str:
    return subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name", "-of", "csv=p=0",
                           str(path)], capture_output=True, check=True, text=True).stdout.strip()


def transcode(src: Path, dst: Path, encoder: str) -> None:
    fps = video.probe(src)["fps"]
    quality = ["-preset", "p5", "-cq", "21"] if encoder == "h264_nvenc" else ["-preset", "veryfast", "-crf", "20"]
    video._run([*video.FF, "-threads", "4", "-i", str(src), "-map", "0:v:0", "-map", "0:a?", "-c:v", encoder, *quality,
                "-pix_fmt", "yuv420p", "-g", str(max(1, round(fps))), "-c:a", "copy", "-movflags", "+faststart", str(dst)])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenes", nargs="*", help="street scene keys (default: every street scene)")
    ap.add_argument("--sva-out", default=str(paths.OUT))
    ap.add_argument("--backup", required=True)
    ap.add_argument("--encoder", default="libx264", choices=("libx264", "h264_nvenc"))
    a = ap.parse_args()
    n = 0
    for sdir in sorted(Path(a.sva_out).glob("st-*")):
        if a.scenes and sdir.name not in a.scenes:
            continue
        m = json.loads((sdir / "manifest.json").read_text())
        for cam in m["cams"]:
            for ch in cam.get("chunks", []):
                src = sdir / ch["uri"]
                if ch["uri"].startswith("s3://") or codec(src) == "h264":
                    continue
                tmp = src.with_suffix(".h264.tmp.mp4")
                transcode(src, tmp, a.encoder)
                keep = Path(a.backup) / sdir.name / ch["uri"]
                keep.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(src, keep)
                tmp.replace(src)
                n += 1
                print(f"transcode_chunks {sdir.name} {ch['uri']}", file=sys.stderr)
    print(f"transcode_chunks: {n} chunks re-encoded to H.264", file=sys.stderr)


if __name__ == "__main__":
    main()
