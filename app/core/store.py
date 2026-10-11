"""Read-only index plus durable application data and scene assets.

Text and visual branches each take top-k by cosine, union by clip_id; on site the live VSS rows mapped to our
clips add a text and a visual branch of their own.
RRF = sum(1 / (60 + rank)); hard filters apply before ranking (CONTRACT §5).
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
import tempfile
import time
from pathlib import Path
from threading import RLock
from typing import Any, Literal
from uuid import uuid4

import duckdb
import numpy as np

from app.core.models import Action, AgentRun, BlobRef, Claim, Clip, Entity, Event, Final, MomentCard, Moment, RunStep, SearchFilters, Zone
from app.settings import Settings

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / 'schemas'
INDEX_TABLES = ('clips', 'detections', 'tracks', 'events', 'gps', 'places', 'scene_manifest')
APP_KEYS = {'agent_runs': 'run_id', 'query_cache': 'qhash', 'actions': 'action_id', 'zones': 'zone_id', 'action_deliveries': ('action_id','channel'),
            'investigations':'session_id', 'investigation_turns':'turn_id', 'investigation_jobs':'job_id',
            'investigation_events':'event_id', 'investigation_observations':'observation_id'}
JSON_COLUMNS = {'context', 'steps', 'claims', 'evidence', 'hits', 'model_revs', 'response', 'polygon_world', 't_range', 'manifest', 'params', 'geom', 'request_json', 'frame_json', 'observation_json'}
VSS_COLUMNS = ['source', 'original_video', 'segment_start_sec', 'segment_end_sec', 'reasoning_content', 'object_counts', 'camera_id', 'location', 'vectors', 'vectors_visual']
logger = logging.getLogger(__name__)


def _key_columns(name):
    if name not in APP_KEYS:
        raise ValueError('Unknown application table')
    key = APP_KEYS[name]
    return (key,) if isinstance(key,str) else key


def _encode_key(name, value):
    keys = _key_columns(name)
    if len(keys)==1:
        return str(value)
    if not isinstance(value,(list,tuple)) or len(value)!=len(keys):
        raise ValueError('Composite key requires all key columns')
    return json.dumps(list(value),separators=(',',':'))


def _decode_key(name, value):
    keys = _key_columns(name)
    values = [value] if len(keys)==1 else json.loads(value)
    if len(values)!=len(keys):
        raise ValueError('Invalid mirror key')
    return values


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=lambda x: x.model_dump(mode='json'))


def now():
    return datetime.now(timezone.utc).isoformat()


def nearest(times, t):
    return min(range(len(times)), key=lambda i: abs(times[i] - t)) if times else None


class Store:
    # {'rows': n, 'text' | 'visual': (clip_ids, unit vectors), 'caption': {clip_id: text},
    #  'counts': {clip_id: {label: max boxes in one frame}}} of the live vss-collection; None until loaded
    live: dict | None = None

    def __init__(self, *, index_path: Path | None, app_path: Path, asset_dir: Path | None,
                 backend: Literal['duckdb', 'vastdb'] = 'duckdb', allow_duckdb_fallback: bool = False) -> None:
        self.index_path, self.app_path, self.asset_dir = index_path, Path(app_path), asset_dir
        self.backend, self.settings = backend, Settings()
        self.runs_dir = self.settings.runs_dir
        self.lock = RLock()
        self._mirror_lock = RLock()
        # per scene_key; the index and scene assets do not change while the process runs
        self._manifests, self._entities, self._events = {}, {}, {}
        self.app_path.parent.mkdir(parents=True, exist_ok=True)
        self.app = duckdb.connect(str(self.app_path))
        self.app.execute((SCHEMAS_DIR / 'app.sql').read_text())
        self.app.execute('CREATE TABLE IF NOT EXISTS mirror_outbox(table_name TEXT, row_key TEXT, generation TEXT, PRIMARY KEY(table_name,row_key))')
        self.app.execute('CREATE TABLE IF NOT EXISTS blob_outbox(uri TEXT PRIMARY KEY, local_path TEXT, sha256 TEXT, size BIGINT, attempts INTEGER, next_retry DOUBLE, generation TEXT)')
        self._blob_lock = RLock()
        self.app.execute('CREATE TABLE IF NOT EXISTS action_deliveries(action_id TEXT, channel TEXT, status TEXT, updated TIMESTAMP, PRIMARY KEY(action_id,channel))')
        self.index = None
        self.closed = False
        try:
            self._repair_actions_event_index()
            if backend == 'vastdb':
                self.index = duckdb.connect(':memory:')
                self.index.execute((SCHEMAS_DIR / 'index.sql').read_text())
                try:
                    self._load_vastdb()
                except Exception:
                    if not allow_duckdb_fallback:
                        raise
                    self.index.close()
                    self.index = None
                    self.backend = 'duckdb'
            if self.index is None:
                self.index = duckdb.connect(str(index_path), read_only=True)
        except Exception:
            self.close()
            raise

    def _repair_actions_event_index(self):
        """Rebuild the explicit ART index once, retaining both data and uniqueness.

        A persisted actions_event index from an earlier release can read normally
        but fatally fail when a LIST update is rewritten to delete/insert. Reopen
        and CHECKPOINT do not rebuild that index. Transactional DDL reconstructs
        it from the authoritative table rows before any action writer runs.
        """
        migration = '2026-10-10-actions-event-art-rebuild-v1'
        if self.app.execute('SELECT 1 FROM app_migrations WHERE name=?',[migration]).fetchone():
            return
        self.app.execute('BEGIN TRANSACTION')
        try:
            # DuckDB cannot drop and recreate an index with the same name in one
            # transaction. Build the replacement first; uniqueness is never absent.
            self.app.execute('CREATE UNIQUE INDEX actions_event_v2 ON actions(scene_key,event_id)')
            self.app.execute('DROP INDEX IF EXISTS actions_event')
            self.app.execute('INSERT INTO app_migrations VALUES (?,?)',[migration,now()])
            self.app.execute('COMMIT')
        except BaseException:
            self.app.execute('ROLLBACK')
            raise

    def close(self):
        with self.lock:
            if not self.closed:
                if self.index is not None:
                    self.index.close()
                self.app.close()
                self.closed = True

    def rows(self, db, sql, params=()):
        with self.lock:
            result = db.execute(sql, params)
            names = [c[0] for c in result.description]
            return [dict(zip(names, r)) for r in result.fetchall()]

    def _connect_vastdb(self):
        import vastdb
        s = self.settings
        config = getattr(vastdb, 'config', None)
        retry = {'backoff_config':config.BackoffConfig(max_tries=1,max_time=10)} if config is not None else {}
        return vastdb.connect(endpoint=s.vdb_endpoint, access=s.access_key, secret=s.secret_key, ssl_verify=s.vdb_verify, timeout=(3,5), **retry)

    def _load_vastdb(self):
        with self._connect_vastdb().transaction() as tx:
            schema = tx.bucket(self.settings.vastdb_bucket).schema('sva')
            for name in (*INDEX_TABLES, *APP_KEYS):
                table = schema.table(name, fail_if_missing=name in INDEX_TABLES)
                if table is None:
                    continue
                arrow = table.select().read_all()
                if name in APP_KEYS:
                    self._restore_app_rows(name, arrow)
                else:
                    self.index.register('_vast_rows', arrow)
                    try:
                        self.index.execute(f'DELETE FROM {name}')
                        self.index.execute(f'INSERT INTO {name} BY NAME SELECT * FROM _vast_rows')
                    finally:
                        self.index.unregister('_vast_rows')

    def _restore_app_rows(self, name, arrow):
        """Local application data is authoritative. Remote snapshots only recover absent keys."""
        if name not in APP_KEYS:
            raise ValueError('Unknown application table')
        keys = _key_columns(name)
        # A final run wins over an earlier running snapshot, regardless of remote row order.
        order = "CASE WHEN status='running' THEN 0 ELSE 1 END DESC, elapsed_ms DESC NULLS LAST," if name == 'agent_runs' else "CASE WHEN status='sent' THEN 2 WHEN status='sending' THEN 1 ELSE 0 END DESC, updated DESC," if name == 'action_deliveries' else ''
        columns = set(arrow.column_names)
        if name == 'investigations' and 'revision' in columns:
            order += 'revision DESC,'
        if 'updated' in columns and name != 'action_deliveries':
            order += 'updated DESC,'
        if 'created' in columns:
            order += 'created DESC,'
        match = ' AND '.join(f'local.{key}=r.{key}' for key in keys)
        partition = ','.join(keys)
        with self.lock:
            self.app.register('_restore_rows', arrow)
            try:
                self.app.execute(f'''INSERT INTO {name} BY NAME
                    SELECT * FROM _restore_rows r
                    WHERE NOT EXISTS (SELECT 1 FROM {name} local WHERE {match})
                    QUALIFY row_number() OVER (PARTITION BY {partition} ORDER BY {order} to_json(r) DESC)=1
                    ON CONFLICT DO NOTHING''')
            finally:
                self.app.unregister('_restore_rows')

    def load_vss_collection(self, maps: tuple[dict, dict]) -> None:
        """Keep the live rows that the site map places on one of our clips; a failed read leaves our clips only."""
        from pipeline.common.site_map import map_hit
        self.live = None
        s = self.settings
        try:
            with self._connect_vastdb().transaction() as tx:
                arrow = tx.bucket(s.vastdb_bucket).schema(s.vdb_schema).table(s.vdb_collection).select(columns=VSS_COLUMNS).read_all()
        except Exception:
            logger.exception('vss-collection read failed; ranking our clips only')
            return
        known = {r['clip_id'] for r in self.rows(self.index, 'SELECT clip_id FROM clips')}
        rows = [(m['clip_id'], r) for r in arrow.to_pylist() if (m := map_hit(maps, r)) and m['clip_id'] in known]
        live = {'rows': len(rows)}
        for branch, column in [('text', 'vectors'), ('visual', 'vectors_visual')]:
            vectors = [(key, np.asarray(r[column], dtype=float)) for key, r in rows if r[column] is not None]
            vectors = [(key, v / np.linalg.norm(v)) for key, v in vectors if np.linalg.norm(v)]
            live[branch] = ([key for key, _ in vectors], np.array([v for _, v in vectors]).reshape(len(vectors), 256))
        live['caption'], live['counts'] = {}, {}
        for key, r in rows:
            if r['reasoning_content']:
                live['caption'].setdefault(key, r['reasoning_content'])
            if r['object_counts'] is not None:
                live['counts'].setdefault(key, json.loads(r['object_counts']))
        self.live = live
        logger.info('vss-collection: %d of %d rows map to our clips', len(rows), arrow.num_rows)

    def queue_mirror(self, name, key):
        """Enqueue inside the caller's local transaction; delivery is always separate."""
        encoded = _encode_key(name, key)
        if self.backend == 'vastdb':
            with self.lock:
                self.app.execute('INSERT OR REPLACE INTO mirror_outbox VALUES (?,?,?)', [name, encoded, uuid4().hex])
        return encoded

    def _mirror(self, name, key):
        encoded = self.queue_mirror(name, key)
        if self.backend == 'vastdb':
            return self._deliver_mirror(name, encoded)
        return True

    def _deliver_mirror(self, name, key):
        # Serialize delivery, not database reads. A newer queued write cannot be erased by an older delivery.
        with self._mirror_lock:
            with self.lock:
                pending = self.app.execute('SELECT generation FROM mirror_outbox WHERE table_name=? AND row_key=?', [name, key]).fetchone()
                if not pending:
                    return True
                keys = _key_columns(name)
                values = _decode_key(name, key)
                predicate = ' AND '.join(f'{col}=?' for col in keys)
                arrow = self.app.execute(f'SELECT * FROM {name} WHERE {predicate}', values).to_arrow_table()
            try:
                self.write_vastdb({name: arrow})
            except Exception:
                logger.warning('VastDB mirror deferred for %s/%s', name, key)
                return False
            with self.lock:
                self.app.execute('DELETE FROM mirror_outbox WHERE table_name=? AND row_key=? AND generation=?', [name, key, pending[0]])
            return True

    def retry_mirrors(self, limit=100, *, should_stop=None):
        """Retry durable pending rows, reading their latest local state rather than stale payloads."""
        if self.backend != 'vastdb':
            return 0
        pending = self.rows(self.app, 'SELECT table_name,row_key FROM mirror_outbox ORDER BY table_name,row_key LIMIT ?', [limit])
        delivered = 0
        for row in pending:
            if should_stop is not None and should_stop():
                break
            delivered += bool(self._deliver_mirror(row['table_name'],row['row_key']))
        return delivered

    def search_clips(self, filters: SearchFilters, qvec_txt: list[float] | None, qvec_vis: list[float] | None) -> list[Clip]:
        where, args = ['workspace=?'], [filters.workspace]
        for col, values in [('scene_key', filters.scene_keys), ('cam_id', filters.cam_ids)]:
            if values is not None:
                if not values:
                    return []
                where.append(f'{col} IN ({",".join("?" for _ in values)})')
                args.extend(values)
        if filters.t_range is not None:
            where.append('t0_ms < ? AND t1_ms > ?')
            args.extend([filters.t_range[1], filters.t_range[0]])
        if filters.place_id is not None:
            where.append('place_id=?')
            args.append(filters.place_id)
        if filters.zone_id is not None:
            where.append('list_contains(zone_ids, ?)')
            args.append(filters.zone_id)
        rows = self.rows(self.index, 'SELECT * FROM clips WHERE ' + ' AND '.join(where) + ' ORDER BY clip_id', args)
        clips = {r['clip_id']: Clip(**{k: v for k, v in r.items() if k not in ('v_txt', 'v_vis')}) for r in rows}
        # a live caption fills a clip the index left without one and never replaces an index caption
        for key, clip in clips.items():
            if clip.caption is None and self.live and key in self.live['caption']:
                clip.caption = self.live['caption'][key]
        # a zero query vector has no direction to rank by, so it reads as no vector
        qvec_txt, qvec_vis = [v if v is not None and any(v) else None for v in (qvec_txt, qvec_vis)]
        union = set()
        def assign(scored, rank, similarity):
            for i, (score, key) in enumerate(sorted(scored, key=lambda x: (-x[0], x[1]))[:filters.k], 1):
                setattr(clips[key], rank, i)
                setattr(clips[key], similarity, score)
                union.add(key)
        for column, vector, rank, similarity in [('v_txt', qvec_txt, 'rank_text', 'sim_text'), ('v_vis', qvec_vis, 'rank_visual', 'sim_visual')]:
            if vector is None:
                continue
            if len(vector) != 256:
                raise ValueError('Query embeddings must have 256 dimensions')
            q = np.asarray(vector, dtype=float)
            scored = []
            for row in rows:
                if row[column] is None:
                    continue
                v = np.asarray(row[column], dtype=float)
                norm = np.linalg.norm(v) * np.linalg.norm(q)
                if norm:
                    scored.append((float(np.dot(v, q) / norm), row['clip_id']))
            assign(scored, rank, similarity)
        for branch, vector, rank, similarity in [('text', qvec_txt, 'rank_vss_text', 'sim_vss_text'), ('visual', qvec_vis, 'rank_vss_visual', 'sim_vss_visual')]:
            if vector is None or not self.live:
                continue
            keys, matrix = self.live[branch]
            q = np.asarray(vector, dtype=float)
            # several live segments can map to one clip; the clip takes its best one
            best = {}
            for key, score in zip(keys, (matrix @ (q / np.linalg.norm(q))).tolist()):
                if key in clips and score > best.get(key, -2):
                    best[key] = score
            assign([(score, key) for key, score in best.items()], rank, similarity)
        if qvec_txt is None and qvec_vis is None:
            return list(clips.values())
        ranks = lambda c: (c.rank_text, c.rank_visual, c.rank_vss_text, c.rank_vss_visual)
        return sorted((clips[k] for k in union), key=lambda c: (-sum(1 / (60 + r) for r in ranks(c) if r), c.clip_id))

    def events_in(self, scene_key: str, t0_ms: int, t1_ms: int) -> list[Event]:
        if scene_key not in self._events:
            rows = self.rows(self.index, 'SELECT * FROM events WHERE scene_key=? ORDER BY t0_ms,event_id', [scene_key])
            self._events[scene_key] = [Event(**{k: json.loads(v) if k == 'params' and isinstance(v, str) else v for k, v in r.items() if k != 'scene_id'}) for r in rows]
        return [e for e in self._events[scene_key] if e.t0_ms < t1_ms and e.t1_ms > t0_ms]

    def moment(self, scene_key: str, t_ms: int) -> Moment:
        m = self.manifest(scene_key)
        if not m['t0_ms'] <= t_ms < m['t1_ms']:
            raise ValueError('Time outside scene')
        assets, persons, forklifts = m['assets'], [], []
        visibility = self.asset_json(scene_key, assets['visibility']) if 'visibility' in assets else None
        observed = {}
        for cam in m['cams']:
            if 'boxes_dir' not in assets:
                continue
            rel = f"{assets['boxes_dir']}/{cam['id']}/{t_ms // 10000:05d}.json"
            try:
                boxes = self.asset_json(scene_key, rel)['rows']
            except FileNotFoundError:
                continue
            idx = nearest(sorted({r[0] for r in boxes}), t_ms)
            if idx is not None:
                times = sorted({r[0] for r in boxes})
                observed[cam['id']] = {r[1] for r in boxes if r[0] == times[idx] and abs(r[0] - t_ms) <= 1000 / m['fps'] / 2}
        entities = {e.entity_id: e for e in self.entities(scene_key)} if 'entities' in assets else {}
        if 'skeleton' in assets:
            skeleton = self.asset_json(scene_key, assets['skeleton'])
            for eid, p in skeleton['persons'].items():
                i = nearest(p['t_ms'], t_ms)
                if i is None or abs(p['t_ms'][i]-t_ms)>1000/skeleton['fps']/2+1:
                    continue
                root, seen = p['root_world'][i], []
                if visibility:
                    v = visibility
                    ix = math.floor((root[0] - v['origin'][0]) / v['cell_m'])
                    iy = math.floor((root[1] - v['origin'][1]) / v['cell_m'])
                    sec = t_ms // 1000 - v['t0_s']
                    if 0 <= ix < v['nx'] and 0 <= iy < v['ny'] and 0 <= sec < len(v['per_sec']):
                        data = base64.b64decode(v['per_sec'][sec])
                        cams = v['cam_bit_order']
                        seen = [c for j, c in enumerate(cams) if data[(iy * v['nx'] + ix) * len(cams) + j] == 1]
                obs = sorted({o['cam_id'] for o in entities[eid].observations if o['t0_ms'] <= t_ms < o['t1_ms'] and o['local_track_id'] in observed.get(o['cam_id'], set())})
                persons.append(dict(entity_id=eid, root_world=root, root_method=p['root_method'][i], sigma_m=p['root_sigma_m'][i], xyz17=p['xyz'][i], seen_by=seen, observed_in=obs))
        if 'forklift' in assets:
            for f in self.asset_json(scene_key, assets['forklift'])['forklifts']:
                i = nearest([r['t_ms'] for r in f['track']], t_ms)
                if i is not None and abs(f['track'][i]['t_ms']-t_ms)<=1000/m['fps']/2+1:
                    forklifts.append({'entity_id': f['entity_id'], **{k: v for k, v in f['track'][i].items() if k != 't_ms'}})
        cams = []
        for c in m['cams']:
            rel = f"{c['thumbs']}/{t_ms // 1000:05d}.jpg" if c['thumbs'] else None
            url = self.asset_url(scene_key, rel) if rel and self.asset_exists(scene_key, rel) else None
            cams.append({'id': c['id'], 'frame_url': url})
        return Moment(t_ms=t_ms, persons=persons, forklift=forklifts, cams=cams, floor_visibility_ref=self.asset_url(scene_key, assets['visibility']) if visibility else None)

    def entities(self, scene_key: str) -> list[Entity]:
        if scene_key not in self._entities:
            self._entities[scene_key] = [Entity.model_validate(e) for e in self.asset_json(scene_key, self.manifest(scene_key)['assets']['entities'])['entities']]
        return list(self._entities[scene_key])

    def index_counts(self) -> dict[str, int]:
        with self.lock:
            counts = {name: self.index.execute(f'SELECT count(*) FROM {name}').fetchone()[0] for name in INDEX_TABLES}
        return {**counts, 'vss_collection': self.live['rows']} if self.live else counts

    def manifest(self, scene_key: str) -> dict:
        if scene_key not in self._manifests:
            rows = self.rows(self.index, 'SELECT manifest FROM scene_manifest WHERE scene_key=?', [scene_key])
            if not rows:
                raise FileNotFoundError('Scene not found')
            self._manifests[scene_key] = json.loads(rows[0]['manifest'])
        return self._manifests[scene_key]

    def local_asset(self, path):
        root = self.asset_dir.resolve()
        file = (root / path).resolve()
        if not file.is_relative_to(root) or not file.is_file():
            raise FileNotFoundError('Asset not found')
        return file

    def s3(self):
        import boto3
        from botocore.config import Config
        s = self.settings
        return boto3.client('s3', endpoint_url=s.s3_endpoint, aws_access_key_id=s.access_key, aws_secret_access_key=s.secret_key, config=Config(signature_version='s3v4',connect_timeout=3,read_timeout=5,retries={'max_attempts':1,'mode':'standard'}), verify=s.s3_verify)

    def asset_exists(self, scene_key, relpath):
        if self.asset_dir is not None:
            return (self.asset_dir / scene_key / relpath).is_file()
        from botocore.exceptions import ClientError
        try:
            self.s3().head_object(Bucket=self.settings.asset_bucket, Key=f'sva/{scene_key}/{relpath}')
            return True
        except ClientError as exc:
            if exc.response['Error']['Code'] in ('404', 'NoSuchKey', 'NotFound'):
                return False
            raise

    def asset_json(self, scene_key: str, relpath: str) -> Any:
        if self.asset_dir is not None:
            return json.loads(self.local_asset(f'{scene_key}/{relpath}').read_bytes())
        from botocore.exceptions import ClientError
        try:
            obj = self.s3().get_object(Bucket=self.settings.asset_bucket, Key=f'sva/{scene_key}/{relpath}')
        except ClientError as exc:
            if exc.response['Error']['Code'] in ('404', 'NoSuchKey', 'NotFound'):
                raise FileNotFoundError('Asset not found') from exc
            raise
        return json.loads(obj['Body'].read())

    def _stable_assets(self, value):
        """Recover legacy signed asset URLs as source identities, without network."""
        from urllib.parse import urlparse, unquote
        if isinstance(value,dict):
            return {k:self._stable_assets(v) for k,v in value.items()}
        if isinstance(value,list):
            return [self._stable_assets(v) for v in value]
        if isinstance(value,str) and value.startswith(('https://','http://')):
            url=urlparse(value)
            endpoint=urlparse(self.settings.s3_endpoint)
            prefix=f'/{self.settings.asset_bucket}/sva/'
            if endpoint.netloc and url.netloc==f'{self.settings.asset_bucket}.{endpoint.netloc}':
                prefix='/sva/'
            if endpoint.netloc and url.netloc in (endpoint.netloc,f'{self.settings.asset_bucket}.{endpoint.netloc}') and url.path.startswith(prefix):
                relative=unquote(url.path[len(prefix):])
                if '..' not in Path(relative).parts:
                    return '/api/assets/'+relative
        return value

    def asset_url(self, scene_key: str, relpath: str) -> str:
        path = f'{scene_key}/{relpath}'
        return '/api/assets/' + path

    def presigned_asset_url(self, scene_key: str, relpath: str) -> str:
        path = f'{scene_key}/{relpath}'
        return self.s3().generate_presigned_url('get_object', Params={'Bucket': self.settings.asset_bucket, 'Key': 'sva/' + path})

    def list_scenes(self, workspace: str | None = None) -> list[dict]:
        rows = self.rows(self.index, 'SELECT manifest FROM scene_manifest' + (' WHERE workspace=?' if workspace else '') + ' ORDER BY scene_key', [workspace] if workspace else [])
        return [json.loads(r['manifest']) for r in rows]

    def _commit_write(self, sql, values, mirrors):
        """One local transaction for application mutation and replication intent."""
        with self.lock:
            self.app.execute('BEGIN TRANSACTION')
            try:
                self.app.execute(sql,values)
                for name,key in mirrors:
                    self.queue_mirror(name,key)
                self.app.execute('COMMIT')
            except BaseException:
                self.app.execute('ROLLBACK')
                raise

    def begin_run(self, q: str, ctx: dict, *, kind: Literal['ask', 'watch'] = 'ask', source_run_id: str | None = None) -> str:
        rid = 'r_' + uuid4().hex
        self._commit_write('INSERT INTO agent_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, NULL)',
                           [rid, kind, q, dumps(ctx), '[]', '[]', '[]', '[]', 'running', source_run_id, '{}', now()], [('agent_runs',rid)])
        self._mirror('agent_runs', rid)
        return rid

    def _set_run(self, run_id, **changes):
        # VastDB gets the run at begin_run and finalize_run only; steps, evidence and hits ride on the final row
        with self.lock:
            if 'steps' in changes:
                steps = []
                for step in changes['steps']:
                    data = step.model_dump(mode='json')
                    if step.blob_ref:
                        data['input'] = data['output'] = None
                    steps.append(data)
                changes['steps'] = steps
            columns = ','.join(f'{name}=?' for name in changes)
            self._commit_write(f'UPDATE agent_runs SET {columns} WHERE run_id=?',
                               [dumps(v) if k in JSON_COLUMNS else v for k, v in changes.items()] + [run_id], [('agent_runs',run_id)])

    def _steps(self, run_id):
        raw = self.app.execute('SELECT steps FROM agent_runs WHERE run_id=?', [run_id]).fetchone()[0]
        return [RunStep.model_validate(s) for s in json.loads(raw)]

    def append_step(self, run_id: str, step: RunStep) -> None:
        with self.lock:
            steps = self._steps(run_id)
            steps.append(step)
            self._set_run(run_id, steps=steps)

    def update_step(self, run_id: str, step: RunStep) -> None:
        with self.lock:
            steps = [step if s.step_id == step.step_id else s for s in self._steps(run_id)]
            self._set_run(run_id, steps=steps)

    def add_evidence(self, run_id: str, evidence: list) -> None:
        with self.lock:
            raw = self.app.execute('SELECT evidence FROM agent_runs WHERE run_id=?', [run_id]).fetchone()[0]
            by_id = {e['eid']: e for e in json.loads(raw)}
            for e in evidence:
                data = e.model_dump(mode='json') if hasattr(e, 'model_dump') else e
                by_id[data['eid']] = data
            self._set_run(run_id, evidence=list(by_id.values()))

    def set_hits(self, run_id: str, hits: list[MomentCard]) -> None:
        self._set_run(run_id, hits=hits)

    def finalize_run(self, run_id: str, status: Literal['done', 'error', 'aborted'], claims: list[Claim], *,
                     final: Final | None, weave_url: str | None = None, elapsed_ms: int | None = None) -> None:
        self._set_run(run_id, status=status, claims=claims, final=final, weave_url=weave_url, elapsed_ms=elapsed_ms)
        try:
            self._mirror('agent_runs', run_id)
        except Exception:
            # the steps cell outgrows VastDB's cell limit (TabularValueTooLarge) on long runs; app.duckdb keeps the run
            logger.exception('VastDB mirror of run %s failed', run_id)

    def get_run(self, run_id: str) -> AgentRun | None:
        with self.lock:
            rows = self.rows(self.app, 'SELECT * FROM agent_runs WHERE run_id=?', [run_id])
            if not rows:
                return None
            row = rows[0]
            row = {k: json.loads(v) if k in JSON_COLUMNS and v is not None else v for k, v in row.items()}
            row['created'] = row['created'].isoformat() + 'Z'
            run = AgentRun.model_validate(self._stable_assets(row))
        for step in run.steps:
            if step.blob_ref:
                payload = json.loads(self.read_blob(step.blob_ref))
                step.input, step.output = self._stable_assets(payload['input']), self._stable_assets(payload['output'])
        return run

    def list_runs(self, scene_key: str | None = None, limit: int = 20) -> list[dict]:
        where = " WHERE json_extract_string(context, '$.scene_key')=?" if scene_key else ''
        rows = self.rows(self.app, 'SELECT run_id FROM agent_runs' + where + ' ORDER BY created DESC LIMIT ?', [scene_key, limit] if scene_key else [limit])
        return [self.get_run(r['run_id']).model_dump(mode='json') for r in rows]

    def cache_get(self, qhash: str) -> dict | None:
        rows = self.rows(self.app, 'SELECT * FROM query_cache WHERE qhash=?', [qhash])
        if not rows:
            return None
        return self._stable_assets({k: json.loads(v) if k in JSON_COLUMNS else v.isoformat() if isinstance(v, datetime) else v for k, v in rows[0].items()})

    def cache_put(self, qhash: str, q: str, context: dict, response: dict, backend: str, source_run_id: str) -> None:
        self._commit_write('INSERT OR REPLACE INTO query_cache VALUES (?,?,?,?,?,?,?)',
                           [qhash,q,dumps(context),dumps(response),now(),backend,source_run_id], [('query_cache',qhash)])
        self._mirror('query_cache', qhash)

    def find_action(self, scene_key: str, event_id: str) -> Action | None:
        rows = self.rows(self.app, 'SELECT * FROM actions WHERE scene_key=? AND event_id=?', [scene_key, event_id])
        return self._model_row(Action, rows[0]) if rows else None

    def _model_row(self, cls, row):
        return cls.model_validate({k: json.loads(v) if k in JSON_COLUMNS and v is not None else v.isoformat() + 'Z' if isinstance(v, datetime) else v for k, v in row.items()})

    def _insert_model(self, name, model, mirror=True):
        data = model.model_dump(mode='json')
        columns = list(data)
        sql=f'INSERT INTO {name} ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})'
        values=[dumps(v) if k in JSON_COLUMNS and v is not None else v for k,v in data.items()]
        if mirror:
            self._commit_write(sql,values,[(name,data[APP_KEYS[name]])])
            self._mirror(name,data[APP_KEYS[name]])
        else:
            # Caller owns the surrounding SQL transaction and queues its rows.
            with self.lock:self.app.execute(sql,values)
        return data[APP_KEYS[name]]

    def insert_action(self, action: Action) -> str:
        return self._insert_model('actions', action)

    def list_actions(self, scene_key: str) -> list[Action]:
        return [self._model_row(Action, r) for r in self.rows(self.app, 'SELECT * FROM actions WHERE scene_key=? ORDER BY created', [scene_key])]

    def insert_zone(self, zone: Zone) -> str:
        return self._insert_model('zones', zone)

    def list_zones(self, scene_key: str) -> list[Zone]:
        return [self._model_row(Zone, r) for r in self.rows(self.app, 'SELECT * FROM zones WHERE scene_key=? ORDER BY created', [scene_key])]

    def write_blob(self, run_id, i, payload):
        """Commit immutable local bytes first; remote copying must never fail a step."""
        raw = dumps(payload).encode()
        sha = hashlib.sha256(raw).hexdigest()
        relative = Path(run_id) / f'step_{i}_{sha}.json'
        path = self.runs_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as pending:
            tmp = Path(pending.name)
            pending.write(raw)
            pending.flush()
            os.fsync(pending.fileno())
        try:
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)
        uri = str(path.resolve())
        if self.settings.mode == 'site':
            uri = f's3://{self.settings.asset_bucket}/sva/runs/{relative.as_posix()}'
            with self.lock:
                self.app.execute('INSERT OR REPLACE INTO blob_outbox VALUES (?,?,?,?,0,0,?)',
                                 [uri,str(path.resolve()),sha,len(raw),uuid4().hex])
        return BlobRef(uri=uri, sha256=sha, size=len(raw))

    def retry_blobs(self, limit=100, *, should_stop=None):
        """Bounded background drain. Network I/O occurs outside the Store lock."""
        delivered = 0
        with self._blob_lock:
            rows = self.rows(self.app, 'SELECT * FROM blob_outbox WHERE next_retry<=? ORDER BY uri LIMIT ?', [time.time(),limit])
            for row in rows:
                if should_stop is not None and should_stop():
                    break
                try:
                    raw = Path(row['local_path']).read_bytes()
                    if len(raw)!=row['size'] or hashlib.sha256(raw).hexdigest()!=row['sha256']:
                        raise ValueError('Trace blob checksum mismatch')
                    bucket,key = row['uri'][5:].split('/',1)
                    self.s3().put_object(Bucket=bucket,Key=key,Body=raw,ContentType='application/json',Metadata={'sha256':row['sha256']})
                except Exception:
                    with self.lock:
                        self.app.execute('UPDATE blob_outbox SET attempts=attempts+1,next_retry=? WHERE uri=? AND generation=?',
                                         [time.time()+min(300,2**min(row['attempts']+1,8)),row['uri'],row['generation']])
                    logger.warning('Trace blob copy deferred')
                else:
                    with self.lock:
                        self.app.execute('DELETE FROM blob_outbox WHERE uri=? AND generation=?',[row['uri'],row['generation']])
                    delivered += 1
        return delivered

    def read_blob(self, ref):
        if ref.uri.startswith('s3://'):
            bucket, key = ref.uri[5:].split('/', 1)
            relative = key.removeprefix('sva/runs/')
            local = self.runs_dir / relative
            raw = local.read_bytes() if local.resolve().is_relative_to(self.runs_dir.resolve()) and local.is_file() else self.s3().get_object(Bucket=bucket, Key=key)['Body'].read()
        else:
            path = Path(ref.uri)
            # Backups relocate RUNS_DIR. Persisted dev absolute paths retain a
            # source identity, but hydrate from the restored run-relative path.
            local = self.runs_dir / path.parent.name / path.name
            raw = (local if local.is_file() else path).read_bytes()
        if len(raw) != ref.size or hashlib.sha256(raw).hexdigest() != ref.sha256:
            raise ValueError('Trace blob checksum mismatch')
        return raw

    def write_vastdb(self, tables: dict[str, Any]) -> dict[str, int]:
        import pyarrow as pa
        counts = {}
        with self._connect_vastdb().transaction() as tx:
            schema = tx.bucket(self.settings.vastdb_bucket).schema('sva')
            for name, arrow in tables.items():
                if name not in (*INDEX_TABLES, *APP_KEYS):
                    raise ValueError('Unknown sva table')
                table = schema.table(name, fail_if_missing=False)
                if table is None:
                    table = schema.create_table(name, arrow.schema)
                if name in APP_KEYS:
                    columns = _key_columns(name)
                    key = columns[0]
                    keys = list(zip(*(arrow[col].to_pylist() for col in columns)))
                    predicate = table[key].isin([value[0] for value in keys])
                    existing = table.select(columns=list(columns), predicate=predicate, internal_row_id=True).read_all()
                    ids = dict(zip(zip(*(existing[col].to_pylist() for col in columns)), existing['$row_id'].to_pylist()))
                    updates = [i for i, k in enumerate(keys) if k in ids]
                    inserts = [i for i, k in enumerate(keys) if k not in ids]
                    if updates:
                        rows = arrow.take(updates)
                        table.update(rows.append_column('$row_id', pa.array([ids[k] for k in zip(*(rows[col].to_pylist() for col in columns))], type=pa.uint64())))
                    if inserts:
                        table.insert(arrow.take(inserts))
                    # app tables count only the rows holding the written keys
                    verified = table.select(columns=list(columns), predicate=predicate).read_all()
                    counts[name] = sum(k in set(keys) for k in zip(*(verified[col].to_pylist() for col in columns)))
                    expected_existing = sum(k in set(keys) for k in zip(*(existing[col].to_pylist() for col in columns)))
                    expected = expected_existing + len(inserts)
                else:
                    count_column = arrow.column_names[0]
                    before = table.select(columns=[count_column]).read_all().num_rows
                    table.insert(arrow)
                    counts[name] = table.select(columns=[count_column]).read_all().num_rows
                    expected = before + arrow.num_rows
                if counts[name] != expected:
                    raise RuntimeError(f'VastDB row count verification failed for {name}')
        return counts
