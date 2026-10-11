"""Independent regressions for durable job state transitions."""
from threading import Event, Thread

from app.core.investigations import Investigations
from app.tests.unit.test_investigations import body, frames, index, store


def test_cancel_after_durable_done_commit_is_a_noop(store, monkeypatch):
    service = Investigations(store, runner=frames)
    job = service.submit(body(service.create()))
    reached, release = Event(), Event()
    original = service.finish

    def delayed_finish(row, status, *args, **kwargs):
        if status == 'done':
            reached.set()
            assert release.wait(4)
        return original(row, status, *args, **kwargs)

    monkeypatch.setattr(service, 'finish', delayed_finish)
    worker = Thread(target=service.execute,
                    args=(service.one('investigation_jobs', 'job_id', job['job_id']),))
    worker.start()
    try:
        assert reached.wait(4)
        requested = service.cancel(job['job_id'])
        assert not requested['cancel_requested'] and requested['status'] == 'done'
    finally:
        release.set()
        worker.join(4)
    assert not worker.is_alive()
    assert service.job(job['job_id'])['status'] == 'done'
    assert service.session(job['session_id'])['turns'][0]['status'] == 'done'


def test_cancel_queued_job_does_not_execute_on_recovery(store):
    service = Investigations(store, runner=lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError('Cancelled queued job must not execute')))
    job = service.submit(body(service.create()))
    assert service.cancel(job['job_id'])['status'] == 'cancelled'
    service.start()
    try:
        assert service.job(job['job_id'])['attempt'] == 0
        assert list(service.stream(job['job_id'])) == []
        assert service.session(job['session_id'])['turns'][0]['status'] == 'cancelled'
    finally:
        service.close()


def test_recovered_attempt_does_not_display_prior_attempt_observations(store):
    from app.core.investigations import encode, ident, stamp

    def new_attempt(s, request, **kwargs):
        for frame in frames(s, request):
            frame['data']['run_id'] = 'r_fedcba9876543210'
            yield frame

    service = Investigations(store, runner=new_attempt)
    job = service.submit(body(service.create()))
    old_run = 'r_0123456789abcdef'
    old_obs = {'observation_id': 'o_' + 'a' * 24, 'text': 'Previous attempt'}
    with service.transaction() as db:
        db.execute("UPDATE investigation_jobs SET status='running',attempt=1,last_seq=1,run_id=?,lease_started=? WHERE job_id=?", [old_run, stamp(), job['job_id']])
        db.execute('INSERT INTO investigation_events VALUES (?,?,?,?,?,?)', [ident('v'), job['job_id'], 1, 1, encode({'event':'observation','data':{'run_id':old_run,'observation':old_obs}}), stamp()])
        db.execute('INSERT INTO investigation_observations VALUES (?,?,?,?,?,?,?,?,?,?,?)', ['old_occurrence',job['session_id'],job['turn_id'],job['job_id'],old_run,'wh-sdg-fixture-01','ceiling_00',0,1000,encode(old_obs),stamp()])
    service.start()
    try:
        from app.tests.unit.test_investigations import await_done
        done = await_done(service, job['job_id'])
        assert done['status'] == 'done' and done['attempt'] == 2, done['error']
        assert done['current_attempt_start_seq'] == 2 and done['last_seq'] == 5
        history = service.session(job['session_id'])['turns'][0]
        assert history['observations'] == []
        assert history['answers'][0]['run_id'] == 'r_fedcba9876543210'
        replay = list(service.stream(job['job_id'], after_seq=1))
        assert len(replay) == 4 and 'Previous attempt' not in ''.join(replay)
        assert service.rows('investigation_observations')[0]['run_id'] == old_run
    finally:
        service.close()


def test_application_mirrors_are_queued_transactionally_without_blocking_execution(store, monkeypatch):
    store.backend = 'vastdb'
    writes = []

    def unavailable(tables):
        writes.append(set(tables))
        raise OSError('Disconnected mirror')

    monkeypatch.setattr(store, 'write_vastdb', unavailable)
    service = Investigations(store, runner=frames)
    job = service.submit(body(service.create()))
    service.execute(service.one('investigation_jobs','job_id',job['job_id']))
    assert service.job(job['job_id'])['status'] == 'done'
    assert writes == []
    pending = store.rows(store.app, 'SELECT * FROM mirror_outbox')
    assert {'investigations','investigation_turns','investigation_jobs','investigation_events'} <= {r['table_name'] for r in pending}
    assert store.retry_mirrors(limit=100) == 0
    assert store.rows(store.app, 'SELECT * FROM mirror_outbox') == pending
    delivered = []
    monkeypatch.setattr(store, 'write_vastdb', lambda rows: delivered.append(rows))
    assert store.retry_mirrors(limit=100) == len(pending)
    assert not store.rows(store.app, 'SELECT * FROM mirror_outbox')
    assert service.session(job['session_id'])['turns'][0]['status'] == 'done'


def interrupted_output(service, job, *, spent_ms=0):
    from app.core.investigations import encode, ident, stamp
    old_run='r_0123456789abcdef'
    old_observation={'observation_id':'o_'+'b'*24,'text':'Retired observation'}
    retired=[{'event':'answer','data':{'run_id':old_run,'text':'Retired answer'}},
             {'event':'observation','data':{'run_id':old_run,'observation':old_observation}}]
    with service.transaction() as db:
        db.execute("UPDATE investigation_jobs SET status='running',attempt=1,last_seq=2,run_id=?,spent_ms=?,lease_started=? WHERE job_id=?", [old_run,spent_ms,stamp(),job['job_id']])
        db.execute("UPDATE investigation_turns SET status='running',run_id=? WHERE turn_id=?", [old_run,job['turn_id']])
        for seq,frame in enumerate(retired,1):
            db.execute('INSERT INTO investigation_events VALUES (?,?,?,?,?,?)',[ident('v'),job['job_id'],seq,1,encode(frame),stamp()])
        db.execute('INSERT INTO investigation_observations VALUES (?,?,?,?,?,?,?,?,?,?,?)',['retired_occurrence',job['session_id'],job['turn_id'],job['job_id'],old_run,'wh-sdg-fixture-01','ceiling_00',0,1000,encode(old_observation),stamp()])


def test_recovery_queued_projection_hides_partial_attempt_before_worker_runs(store,monkeypatch):
    service=Investigations(store,runner=frames)
    job=service.submit(body(service.create()))
    interrupted_output(service,job)
    release=Event()
    monkeypatch.setattr(service,'work',lambda:release.wait(4))
    service.start()
    try:
        queued=service.job(job['job_id'])
        assert queued['status']=='queued' and queued['run_id'] is None
        assert queued['current_attempt_start_seq']==3
        turn=service.session(job['session_id'])['turns'][0]
        assert turn['run_id'] is None and turn['answers']==[] and turn['observations']==[]
        subscription=service.stream(job['job_id'])
        assert next(subscription)==': keepalive\n\n'
        subscription.close()
        # The retired evidence remains available for audit, not active display.
        assert len(service.rows('investigation_events'))==2
        assert len(service.rows('investigation_observations'))==1
    finally:
        release.set();service.close()


def test_recovery_exhausted_budget_stream_terminates_without_retired_output(store):
    from app.tests.unit.test_investigations import await_done
    service=Investigations(store,runner=lambda *a,**kw: (_ for _ in ()).throw(
        AssertionError('Exhausted recovery must not execute')))
    job=service.submit(body(service.create()))
    interrupted_output(service,job,spent_ms=25000)
    service.start()
    try:
        failed=await_done(service,job['job_id'])
        assert failed['status']=='failed' and failed['current_attempt_start_seq']==3
        subscription=service.stream(job['job_id'])
        try:
            import pytest
            with pytest.raises(StopIteration):next(subscription)
        finally:subscription.close()
        turn=service.session(job['session_id'])['turns'][0]
        assert turn['answers']==[] and turn['observations']==[]
    finally:service.close()
