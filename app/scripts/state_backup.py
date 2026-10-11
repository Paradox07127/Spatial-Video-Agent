"""Offline single-writer backup / clean restore of application DB and run blobs.

Stop Uvicorn first. Never use this tool against a running app. Asset/index releases
are separately immutable inputs, not part of this application-state snapshot.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
import sys
import tempfile

import duckdb


def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as src:
        while block:=src.read(1024*1024):h.update(block)
    return h.hexdigest()


def backup(app_path:Path,runs_dir:Path,dest:Path):
    if dest.exists():raise ValueError('Backup destination must not exist')
    if not app_path.is_file():raise ValueError('Application database is missing')
    if app_path.is_symlink() or runs_dir.is_symlink():
        raise ValueError('State paths must not be symlinks')
    if dest.resolve().is_relative_to(runs_dir.resolve()):
        raise ValueError('Backup destination must be outside the run directory')
    if runs_dir.exists() and any(p.is_symlink() for p in runs_dir.rglob('*')):
        raise ValueError('Run directory must not contain symlinks')
    dest.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix='.sva-backup-',dir=dest.parent))
    try:
        # Holding DuckDB's writer lock prevents another process entering while
        # the checkpointed database and immutable blob files are copied.
        with duckdb.connect(str(app_path)) as db:
            db.execute('CHECKPOINT')
            tables={name:db.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
                    for (name,) in db.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall()}
            shutil.copy2(app_path,stage/'app.duckdb')
            if runs_dir.exists():shutil.copytree(runs_dir,stage/'runs')
            else:(stage/'runs').mkdir()
        files={p.relative_to(stage).as_posix():{'size':p.stat().st_size,'sha256':digest(p)}
               for p in sorted(stage.rglob('*')) if p.is_file()}
        (stage/'manifest.json').write_text(json.dumps({'version':1,'tables':tables,'files':files},indent=2)+'\n')
        stage.rename(dest)
    finally:
        if stage.exists():shutil.rmtree(stage)
    return {'tables':tables,'files':len(files)}


def restore(src:Path,app_path:Path,runs_dir:Path):
    if app_path.resolve().is_relative_to(src.resolve()) or runs_dir.resolve().is_relative_to(src.resolve()):
        raise ValueError('Restore destination must be outside the snapshot')
    if app_path.exists() or runs_dir.exists():raise ValueError('Restore requires clean application database and run directory')
    manifest=json.loads((src/'manifest.json').read_text())
    if manifest['version']!=1:raise ValueError('Unsupported backup version')
    actual={p.relative_to(src).as_posix() for p in src.rglob('*') if p.is_file() and p.name!='manifest.json'}
    if actual!=set(manifest['files']):raise ValueError('Backup file set mismatch')
    for rel,expected in manifest['files'].items():
        p=src/rel
        if not p.resolve().is_relative_to(src.resolve()) or p.is_symlink() or p.stat().st_size!=expected['size'] or digest(p)!=expected['sha256']:
            raise ValueError('Backup checksum mismatch')
    app_path.parent.mkdir(parents=True,exist_ok=True)
    runs_dir.parent.mkdir(parents=True,exist_ok=True)
    # Validation precedes every destination write. Failures remove only files
    # this restore created; existing state is never overwritten.
    try:
        shutil.copytree(src/'runs',runs_dir)
        shutil.copy2(src/'app.duckdb',app_path)
        with duckdb.connect(str(app_path)) as db:
            tables={r[0] for r in db.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall()}
            if 'blob_outbox' in tables:
                for uri,old in db.execute('SELECT uri,local_path FROM blob_outbox').fetchall():
                    old=Path(old)
                    db.execute('UPDATE blob_outbox SET local_path=? WHERE uri=?',[str((runs_dir/old.parent.name/old.name).resolve()),uri])
            db.execute('CHECKPOINT')
        weave=runs_dir/'outbox.jsonl'
        if weave.is_file():
            rows=[json.loads(line) for line in weave.read_text().splitlines()]
            for row in rows:row['app_path']=str(app_path.resolve())
            weave.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    except BaseException:
        app_path.unlink(missing_ok=True)
        Path(str(app_path)+'.wal').unlink(missing_ok=True)
        if runs_dir.exists():shutil.rmtree(runs_dir)
        raise
    return {'tables':manifest['tables'],'files':len(actual)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=['backup','restore'])
    parser.add_argument('snapshot',type=Path)
    parser.add_argument('--app',type=Path,required=True)
    parser.add_argument('--runs',type=Path,required=True)
    args=parser.parse_args()
    result=backup(args.app,args.runs,args.snapshot) if args.operation=='backup' else restore(args.snapshot,args.app,args.runs)
    print(json.dumps(result,sort_keys=True))


if __name__=='__main__':main()
