"""Recorded agent fixtures must not depend on a prior media-rendering pass."""
from pathlib import Path
import shutil
import subprocess
import sys

SVA = Path(__file__).resolve().parents[3]


def test_corpus_recording_is_identical_with_and_without_existing_media(tmp_path):
    out=tmp_path/'fixtures'
    command=[sys.executable,str(SVA/'fixtures/gen_fixture.py'),'--no-media','--out',str(out)]
    subprocess.run(command,check=True,capture_output=True)
    recorded=(out/'l1/asks_corpus.json').read_bytes()
    copied=0
    # Use the real committed media, never placeholder bytes that pretend to be frames.
    for source in (SVA/'fixtures').rglob('*'):
        if source.is_file() and source.suffix.lower() in {'.jpg','.jpeg','.png','.mp4','.mov','.webm'}:
            destination=out/source.relative_to(SVA/'fixtures')
            destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(source,destination)
            copied+=1
    assert copied>0
    subprocess.run(command,check=True,capture_output=True)
    assert (out/'l1/asks_corpus.json').read_bytes()==recorded
    assert any(out.rglob('*.jpg'))  # generation preserved the caller's real media
