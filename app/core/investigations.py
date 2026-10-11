"""Single-writer durable investigations and resumable SSE subscriptions.

Subscriptions never own execution. A process restart retries only unfinished
turns, charging the abandoned attempt against its original bounded budget.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
from threading import Condition, Event, Thread
import time
from uuid import uuid4

from fastapi import HTTPException

log = logging.getLogger(__name__)
TERMINAL = {'done', 'failed', 'cancelled'}


def stamp():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def ident(prefix):
    return prefix + '_' + uuid4().hex[:24]


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=str)


class Investigations:
    def __init__(self, store, *, vss=None, runner=None):
        self.store, self.vss, self.runner = store, vss, runner
        self.changed = Condition()
        self.closing = Event()
        self.thread = None
        material = store.rows(store.index, 'SELECT scene_key,manifest FROM scene_manifest ORDER BY scene_key')
        self.release_id = hashlib.sha256(encode(material).encode()).hexdigest()

    @contextmanager
    def transaction(self):
        with self.store.lock:
            self.store.app.execute('BEGIN TRANSACTION')
            try:
                yield self.store.app
                self.store.app.execute('COMMIT')
            except BaseException:
                self.store.app.execute('ROLLBACK')
                raise
        with self.changed:
            self.changed.notify_all()

    def mirror(self, table, key):
        # Store queues in the same local transaction; maintenance drains outside
        # the Store lock and outside request/worker critical paths.
        self.store.queue_mirror(table, key)

    def rows(self, table, clause='', params=()):
        rows = self.store.rows(self.store.app, 'SELECT * FROM ' + table + ' ' + clause, params)
        for row in rows:
            for col in ('context', 'request_json', 'frame_json', 'observation_json'):
                if isinstance(row.get(col), str):
                    row[col] = json.loads(row[col])
            for col in ('created', 'updated', 'lease_started'):
                if isinstance(row.get(col), datetime):
                    row[col] = row[col].isoformat() + 'Z'
        return rows

    def one(self, table, field, value):
        rows = self.rows(table, 'WHERE ' + field + '=?', [value])
        if not rows:
            raise HTTPException(404, 'Investigation record not found')
        return rows[0]

    def create(self, title='Untitled investigation'):
        sid = ident('s')
        with self.transaction() as db:
            db.execute('INSERT INTO investigations VALUES (?,?,?,?,?,?)',
                       [sid, title, 0, self.release_id, stamp(), stamp()])
            self.mirror('investigations', sid)
        return self.one('investigations', 'session_id', sid)

    def sessions(self):
        return self.rows('investigations', 'ORDER BY updated DESC LIMIT 100')

    def session(self, sid):
        session = self.one('investigations', 'session_id', sid)
        turns = self.rows('investigation_turns', 'WHERE session_id=? ORDER BY created,turn_id', [sid])
        for turn in turns:
            job = self.job(turn['job_id'])
            turn.update(status=job['status'], attempt=job['attempt'], last_seq=job['last_seq'])
            frames = self.rows('investigation_events', 'WHERE job_id=? AND attempt=? AND seq>=? ORDER BY seq',
                               [job['job_id'], job['attempt'],job['current_attempt_start_seq']])
            turn['answers'] = [r['frame_json']['data'] for r in frames if r['frame_json']['event'] == 'answer']
            turn['observations'] = [r['observation_json'] for r in self.rows('investigation_observations',
                                    'WHERE turn_id=? AND run_id=? ORDER BY created,observation_id', [turn['turn_id'],job['run_id']])]
        session['turns'] = turns
        return session

    def job(self, jid):
        row = self.one('investigation_jobs', 'job_id', jid)
        return {k: v for k, v in row.items() if k != 'request_json'}

    def submit(self, body):
        request = body.model_dump(mode='json')
        submitted = json.loads(encode(request))
        # Omission inherits, while explicit null clears. model_dump alone loses
        # that distinction and must not collapse different idempotent requests.
        submitted['_context_fields'] = sorted(body.context.model_fields_set)
        sid, parent, rid = request['session_id'], request.get('parent_turn_id'), request.get('request_id')
        if not rid or request.get('expected_revision') is None:
            raise HTTPException(422, 'Persistent asks require request_id and expected_revision')
        with self.transaction() as db:
            session = self.one('investigations', 'session_id', sid)
            previous = self.rows('investigation_turns', 'WHERE session_id=? AND request_id=?', [sid, rid])
            if previous:
                old = self.one('investigation_jobs', 'job_id', previous[0]['job_id'])
                if old['request_json'].get('_submitted', old['request_json']) != submitted:
                    raise HTTPException(409, 'request_id was already used for a different request')
                return self.job(old['job_id'])
            if session['release_id'] != self.release_id:
                raise HTTPException(409, 'Source release changed; start a new investigation')
            if request['expected_revision'] != session['revision']:
                raise HTTPException(409, 'Investigation revision changed; reload its history')
            if parent:
                turn = self.one('investigation_turns', 'turn_id', parent)
                if turn['session_id'] != sid:
                    raise HTTPException(409, 'Parent turn belongs to a different investigation')
                if turn['status'] not in TERMINAL:
                    raise HTTPException(409, 'Parent turn is still running')
                supplied = body.context.model_fields_set
                previous_context = turn['context']
                changed_source = any(k in supplied and request['context'].get(k) != previous_context.get(k)
                                     for k in ('workspace', 'scene_key', 'scene_id', 'sources'))
                for key, value in previous_context.items():
                    if key not in supplied and not (changed_source and key in ('scene_key','scene_id','cam_id','t_ms','t_range','selection','zone')):
                        request['context'][key] = value
            from app.api.agent import AskContext
            context = AskContext.model_validate(request['context']).model_dump(mode='json')
            if context['workspace'] is None:
                raise HTTPException(422, 'Investigation requires a workspace or a parent that supplies it')
            key = context.get('scene_key') or (context['scene_id'].replace('/', '-').lower() if context.get('scene_id') else None)
            if key:
                try:
                    manifest = self.store.manifest(key)
                except FileNotFoundError:
                    raise HTTPException(422, 'context scene does not exist') from None
                if manifest['workspace'] != context['workspace']:
                    raise HTTPException(422, 'context scene does not belong to workspace')
                if context.get('cam_id') and context['cam_id'] not in {c['id'] for c in manifest['cams']}:
                    raise HTTPException(422, 'context camera does not belong to scene')
            request['context'] = context
            if len(self.rows('investigation_jobs', "WHERE status IN ('queued','running') LIMIT 64")) >= 64:
                raise HTTPException(429, 'Investigation queue is full')
            tid, jid, now = ident('t'), ident('j'), stamp()
            request['_submitted'] = submitted
            db.execute('INSERT INTO investigation_turns VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       [tid, sid, parent, rid, request['q'], encode(request['context']), jid, None, 'queued', None, now])
            db.execute('INSERT INTO investigation_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       [jid, sid, tid, encode(request), 'queued', 0, 0, 1, None, 0, 0, None, False, None, now, now])
            db.execute('UPDATE investigations SET revision=revision+1,updated=? WHERE session_id=?', [now, sid])
            for table, key in (('investigations', sid), ('investigation_turns', tid), ('investigation_jobs', jid)):
                self.mirror(table, key)
        return self.job(jid)

    def cancel(self, jid):
        with self.transaction() as db:
            job = self.job(jid)
            if job['status'] not in TERMINAL:
                status = 'cancelled' if job['status'] == 'queued' else job['status']
                db.execute('UPDATE investigation_jobs SET cancel_requested=TRUE,status=?,updated=? WHERE job_id=?',
                           [status, stamp(), jid])
                if status == 'cancelled':
                    db.execute("UPDATE investigation_turns SET status='cancelled' WHERE turn_id=?", [job['turn_id']])
                    self.mirror('investigation_turns', job['turn_id'])
                self.mirror('investigation_jobs', jid)
        return self.job(jid)

    def start(self):
        with self.transaction() as db:
            for job in self.rows('investigation_jobs', "WHERE status='running'"):
                request = job['request_json']
                budget_ms = int(request['budget']['seconds'] * 1000)
                # A killed process cannot checkpoint its in-flight call. Charge
                # that lease conservatively; an outage never grants free work.
                lease = datetime.fromisoformat(job['lease_started'].rstrip('Z')) if job['lease_started'] else stamp()
                charged = min(budget_ms, job['spent_ms'] + max(0, int((stamp()-lease).total_seconds()*1000)))
                status = 'cancelled' if job['cancel_requested'] else 'queued'
                db.execute('UPDATE investigation_jobs SET status=?,spent_ms=?,lease_started=NULL,updated=? WHERE job_id=?',
                           [status, charged, stamp(), job['job_id']])
                db.execute('UPDATE investigation_turns SET status=? WHERE turn_id=?', [status, job['turn_id']])
                if status == 'queued':
                    # Retired partial output is audit history, not the restored
                    # turn's current projection while it waits for a new lease.
                    db.execute('UPDATE investigation_jobs SET run_id=NULL,current_attempt_start_seq=last_seq+1 WHERE job_id=?', [job['job_id']])
                    db.execute('UPDATE investigation_turns SET run_id=NULL,final=NULL WHERE turn_id=?', [job['turn_id']])
                self.mirror('investigation_jobs', job['job_id'])
                self.mirror('investigation_turns', job['turn_id'])
        self.thread = Thread(target=self.work, name='investigations', daemon=True)
        self.thread.start()

    def close(self):
        self.closing.set()
        with self.changed:
            self.changed.notify_all()
        if self.thread:
            # All tool calls inherit a maximum 45 s deadline. Do not close the
            # database until the worker has relinquished it.
            self.thread.join(50)
            if self.thread.is_alive():
                raise RuntimeError('Investigation worker did not release the store')

    def work(self):
        while not self.closing.is_set():
            pending = self.rows('investigation_jobs', "WHERE status='queued' ORDER BY created LIMIT 1")
            if pending:
                try:
                    self.execute(pending[0])
                except Exception:
                    log.exception('Investigation worker failed')
            else:
                with self.changed:
                    self.changed.wait(timeout=1)

    def finish(self, job, status, error=None, final=None):
        with self.transaction() as db:
            current = self.job(job['job_id'])
            if current['status'] in TERMINAL:
                return
            if current['cancel_requested']:
                status, error, final = 'cancelled', None, None
            db.execute('UPDATE investigation_jobs SET status=?,error=?,lease_started=NULL,updated=? WHERE job_id=?',
                       [status, error, stamp(), job['job_id']])
            db.execute('UPDATE investigation_turns SET status=?,final=? WHERE turn_id=?', [status, final, job['turn_id']])
            self.mirror('investigation_jobs', job['job_id'])
            self.mirror('investigation_turns', job['turn_id'])

    def execute(self, job):
        from app.api.agent import AskRequest, ask_frames
        request = job['request_json']
        if self.one('investigations', 'session_id', job['session_id'])['release_id'] != self.release_id:
            self.finish(job, 'failed', 'Source release changed; start a new investigation')
            return
        remaining = request['budget']['seconds'] - job['spent_ms'] / 1000
        tools_left = request['budget']['tools'] - job['tools_used']
        if job['attempt'] > 0 and (remaining <= 0 or tools_left <= 0):
            self.finish(job, 'failed', 'Original investigation budget exhausted during recovery')
            return
        request = {k: v for k, v in request.items() if k != '_submitted'}
        request['budget'] = {'seconds': remaining, 'tools': tools_left}
        with self.transaction() as db:
            current = self.job(job['job_id'])
            if current['cancel_requested'] or current['status'] != 'queued':
                return
            job['attempt'] += 1
            db.execute("UPDATE investigation_jobs SET status='running',attempt=?,current_attempt_start_seq=last_seq+1,lease_started=?,updated=? WHERE job_id=?",
                       [job['attempt'], stamp(), stamp(), job['job_id']])
            db.execute("UPDATE investigation_turns SET status='running' WHERE turn_id=?", [job['turn_id']])
            self.mirror('investigation_jobs', job['job_id'])
            self.mirror('investigation_turns', job['turn_id'])
        started, initial_spent = time.monotonic(), job['spent_ms']
        iterator = None
        final, completed = None, False
        try:
            iterator = iter((self.runner or ask_frames)(self.store, AskRequest.model_validate(request), vss=self.vss))
            for frame in iterator:
                if self.closing.is_set():
                    return  # running row is recoverable on next process startup
                with self.transaction() as db:
                    current = self.job(job['job_id'])
                    if current['cancel_requested']:
                        break
                    seq, eid = current['last_seq'] + 1, ident('v')
                    run_id = frame['data']['run_id']
                    db.execute('INSERT INTO investigation_events VALUES (?,?,?,?,?,?)',
                               [eid, job['job_id'], seq, job['attempt'], encode(frame), stamp()])
                    self.mirror('investigation_events', eid)
                    spent = initial_spent + round((time.monotonic()-started)*1000)
                    increment = int(frame['event'] == 'tool_call')
                    db.execute('UPDATE investigation_jobs SET last_seq=?,run_id=?,spent_ms=?,tools_used=tools_used+?,lease_started=?,updated=? WHERE job_id=?',
                               [seq, run_id, spent, increment, stamp(), stamp(), job['job_id']])
                    db.execute('UPDATE investigation_turns SET run_id=? WHERE turn_id=?', [run_id, job['turn_id']])
                    if frame['event'] == 'observation':
                        obs = frame['data']['observation']
                        # Observation content IDs may recur in another turn. The
                        # assertion occurrence is scoped by turn, not overwritten.
                        occurrence = obs['observation_id'] + '_' + job['turn_id'][2:] + '_' + str(job['attempt'])
                        db.execute('INSERT OR IGNORE INTO investigation_observations VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                                   [occurrence, job['session_id'], job['turn_id'], job['job_id'], run_id,
                                    obs['scene_key'], obs['cam_id'], obs['t0_ms'], obs['t1_ms'], encode(obs), stamp()])
                        self.mirror('investigation_observations', occurrence)
                    if frame['event'] == 'status':
                        final = frame['data']['final']
                    completed = frame['event'] == 'done'
                    if completed:
                        db.execute("UPDATE investigation_jobs SET status='done',lease_started=NULL,error=NULL WHERE job_id=?", [job['job_id']])
                        db.execute("UPDATE investigation_turns SET status='done',final=? WHERE turn_id=?", [final, job['turn_id']])
                    self.mirror('investigation_jobs', job['job_id'])
                    self.mirror('investigation_turns', job['turn_id'])
                if completed:
                    break
            if self.job(job['job_id'])['cancel_requested']:
                self.finish(job, 'cancelled')
            elif completed:
                self.finish(job, 'done', final=final)
            else:
                self.finish(job, 'failed', 'Execution ended without a final event')
        except Exception as exc:
            if self.job(job['job_id'])['cancel_requested']:
                self.finish(job, 'cancelled')
            else:
                self.finish(job, 'failed', type(exc).__name__ + ': ' + str(exc)[:300])
        finally:
            close = getattr(iterator, 'close', None)
            if close:
                close()

    def stream(self, jid, after_seq=0):
        self.job(jid)
        cursor = after_seq
        while not self.closing.is_set():
            current = self.job(jid)
            rows = self.rows('investigation_events', 'WHERE job_id=? AND seq>? AND seq>=? ORDER BY seq LIMIT 256',
                             [jid, cursor,current['current_attempt_start_seq']])
            for row in rows:
                cursor = row['seq']
                frame = row['frame_json']
                yield f"id: {cursor}\nevent: {frame['event']}\ndata: {encode(frame['data'])}\n\n"
            job = self.job(jid)
            if job['status'] in TERMINAL and max(cursor,job['current_attempt_start_seq']-1) >= job['last_seq']:
                return
            with self.changed:
                self.changed.wait(timeout=1)
            if not rows:
                yield ': keepalive\n\n'
