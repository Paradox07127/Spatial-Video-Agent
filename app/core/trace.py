"""Persist steps before execution; deliver the same tree to Weave best-effort."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import json
from pathlib import Path
from threading import RLock, Thread
import time
from typing import Any, Iterator, Literal

from app.core.models import AgentState, Claim, ExecState, Final, RunStep
from app.core.store import dumps
from app.settings import Settings

INLINE_LIMIT_BYTES = 8192  # CONTRACT §6.1: larger inputs/outputs are stored in step_<i>.json
_OUTBOX_LOCK = RLock()


@dataclass
class RunHandle:
    run_id: str
    store: Any
    q: str
    context: dict
    kind: Literal['ask', 'watch'] = 'ask'
    t0_monotonic_ms: float = 0.0
    weave_root: Any = None
    next_i: int = 0
    read_eids: set[str] = field(default_factory=set)
    weave_client: Any = None
    weave_calls: dict = field(default_factory=dict)
    weave_enabled: bool = False
    weave_failed: bool = False


class StepHandle:
    def __init__(self, run, step):
        self.run, self.step = run, step
        self.step_id, self.i = step.step_id, step.i

    def output(self, value: Any) -> None:
        self.step.output = value

    def evidence(self, eids: list[str]) -> None:
        self.step.evidence_ids = list(dict.fromkeys(self.step.evidence_ids + eids))
        self.run.read_eids.update(eids)

    def note(self, text: str) -> None:
        self.step.note = text

    def exec_state(self, state: ExecState) -> None:
        self.step.exec_state = state


def _client():
    import weave
    return weave.init(Settings().weave_project, settings={'print_call_link': False})


def _url(call):
    url = call.ui_url
    return url() if callable(url) else url


def _queue(run):
    path = run.store.runs_dir / 'outbox.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {'run_id': run.run_id, 'index_path': str(run.store.index_path) if run.store.index_path else None,
           'app_path': str(run.store.app_path.resolve()), 'asset_dir': str(run.store.asset_dir) if run.store.asset_dir else None,
           'backend': run.store.backend}
    with _OUTBOX_LOCK:
        rows = [json.loads(s) for s in path.read_text().splitlines()] if path.exists() else []
        if not any(r['run_id'] == run.run_id for r in rows):
            with path.open('a') as f:
                f.write(dumps(row) + '\n')


def _failed(run):
    run.weave_failed = True
    _queue(run)


def _stored_step(run, step):
    saved = step.model_copy(deep=True)
    large_in = len(dumps(step.input).encode()) > INLINE_LIMIT_BYTES
    large_out = len(dumps(step.output).encode()) > INLINE_LIMIT_BYTES
    if large_in or large_out:
        saved.blob_ref = run.store.write_blob(run.run_id, step.i, {'input': step.input, 'output': step.output})
        if large_in:
            saved.input = None
        if large_out:
            saved.output = None
    return saved


def start_run(store: Any, q: str, context: dict, *, kind: Literal['ask', 'watch'] = 'ask',
              source_run_id: str | None = None) -> RunHandle:
    rid = store.begin_run(q, context, kind=kind, source_run_id=source_run_id)
    run = RunHandle(rid, store, q, context, kind, time.monotonic() * 1000)
    s = Settings()
    run.weave_enabled = bool(s.wandb_api_key and s.weave_project)
    if run.weave_enabled:
        try:
            run.weave_client = _client()
            run.weave_root = run.weave_client.create_call('sva.ask', {'run_id': rid, 'q': q, 'context': context})
        except Exception:
            _failed(run)
    return run


@contextmanager
def traced(run: RunHandle, name: str, inputs: dict, *, state: AgentState,
           parent_step_id: str | None = None) -> Iterator[StepHandle]:
    i = run.next_i
    run.next_i += 1
    step = RunStep(step_id=f'{run.run_id}.s{i}', parent_step_id=parent_step_id, i=i, state=state, name=name,
                   started_ms=int(time.time() * 1000), elapsed_ms=0, input=inputs, output=None, evidence_ids=[],
                   exec_state='waiting', trace_delivery_state='weave_pending' if run.weave_enabled else 'disabled')
    stored = _stored_step(run, step)
    run.store.append_step(run.run_id, stored)
    call = None
    if run.weave_enabled and not run.weave_failed:
        try:
            parent = run.weave_calls.get(parent_step_id, run.weave_root)
            call = run.weave_client.create_call(name, inputs, parent=parent)
            run.weave_calls[step.step_id] = call
            step.weave_call_id = call.id
        except Exception:
            _failed(run)
    handle = StepHandle(run, step)
    started = time.monotonic()
    error = None
    try:
        yield handle
        if step.exec_state == 'waiting':
            step.exec_state = 'live'
    except BaseException as exc:
        error = exc
        step.exec_state = 'failed'
        step.note = type(exc).__name__
        raise
    finally:
        step.elapsed_ms = int((time.monotonic() - started) * 1000)
        if run.weave_enabled:
            step.trace_delivery_state = 'weave_failed' if run.weave_failed else 'weave_pending'
        stored = _stored_step(run, step)
        run.store.update_step(run.run_id, stored)
        if call is not None:
            try:
                run.weave_client.finish_call(call, output=step.output, exception=RuntimeError(type(error).__name__) if error else None)
                step.trace_delivery_state = 'weave_ok'
            except Exception:
                _failed(run)
                step.trace_delivery_state = 'weave_failed'
            with run.store.lock:
                run.store.update_step(run.run_id, _stored_step(run, step))


def _deliver(run, status, final, claims, elapsed):
    url = None
    if run.weave_root is not None:
        try:
            run.weave_client.finish_call(run.weave_root, output={'status': status, 'final': final, 'claims': [c.model_dump(mode='json') for c in claims]})
            run.weave_client.flush()
            url = _url(run.weave_root)
        except Exception:
            _failed(run)
    if run.weave_failed:
        # one lock over read and rewrite, so a step the request thread updates meanwhile is not overwritten with a stale copy
        with run.store.lock:
            for step in run.store._steps(run.run_id):
                step.trace_delivery_state = 'weave_failed'
                run.store.update_step(run.run_id, _stored_step(run, step))
    run.store.finalize_run(run.run_id, status, claims, final=final, weave_url=url, elapsed_ms=elapsed)
    return url


def finish_run(run: RunHandle, status: Literal['done', 'error', 'aborted'], *, final: Final | None,
               claims: list[Claim]) -> str | None:
    elapsed = int(time.monotonic() * 1000 - run.t0_monotonic_ms)
    if run.weave_root is None:
        return _deliver(run, status, final, claims, elapsed)
    try:
        url = _url(run.weave_root)  # built from project and call id, no network
    except Exception:
        url = None
    run.store.finalize_run(run.run_id, status, claims, final=final, weave_url=url, elapsed_ms=elapsed)
    # Weave flush waits on wandb.ai; it must not hold the SSE stream
    Thread(target=_deliver, args=(run, status, final, claims, elapsed), daemon=True, name=f'weave-{run.run_id}').start()
    return url


def flush_outbox(max_items: int = 100) -> int:
    from app.core.store import Store
    s = Settings()
    path = s.runs_dir / 'outbox.jsonl'
    if not (s.wandb_api_key and s.weave_project and path.exists()):
        return 0
    delivered = 0
    with _OUTBOX_LOCK:
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        remaining = []
        for i, row in enumerate(rows):
            if i >= max_items:
                remaining.append(row)
                continue
            store = None
            try:
                store = Store(index_path=Path(row['index_path']) if row['index_path'] else None, app_path=Path(row['app_path']),
                              asset_dir=Path(row['asset_dir']) if row['asset_dir'] else None, backend=row['backend'])
                run = store.get_run(row['run_id'])
                if run.status == 'running':
                    remaining.append(row)
                    continue
                client = _client()
                root = client.create_call('sva.ask', {'run_id': run.run_id, 'q': run.q, 'context': run.context})
                calls = {}
                for step in run.steps:
                    if step.blob_ref:
                        payload = json.loads(store.read_blob(step.blob_ref))
                        inputs, output = payload['input'], payload['output']
                    else:
                        inputs, output = step.input, step.output
                    call = client.create_call(step.name, inputs, parent=calls.get(step.parent_step_id, root))
                    calls[step.step_id] = call
                    client.finish_call(call, output=output)
                    step.weave_call_id = call.id
                client.finish_call(root, output={'status': run.status, 'final': run.final, 'claims': [c.model_dump(mode='json') for c in run.claims]})
                client.flush()
                for step in run.steps:
                    step.trace_delivery_state = 'weave_ok'
                    store.update_step(run.run_id, step)
                store.finalize_run(run.run_id, run.status, run.claims, final=run.final, weave_url=_url(root), elapsed_ms=run.elapsed_ms)
                delivered += 1
            except Exception:
                remaining.append(row)
            finally:
                if store:
                    store.close()
        path.write_text(''.join(dumps(row) + '\n' for row in remaining))
    return delivered
