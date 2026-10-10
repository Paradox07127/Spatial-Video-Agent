"""A monotonic budget shared by nested I/O within an investigation tool."""
from contextlib import contextmanager
from contextvars import ContextVar
import time

_current = ContextVar('investigation_deadline',default=None)


class DeadlineExceeded(TimeoutError):
    pass


def remaining(default, deadline=None):
    end = deadline if deadline is not None else _current.get()
    value = default if end is None else min(default,end-time.monotonic())
    if value<=0:
        raise DeadlineExceeded('Investigation budget exhausted')
    return value


@contextmanager
def scope(deadline):
    existing = _current.get()
    end = min(existing,deadline) if existing is not None and deadline is not None else existing if deadline is None else deadline
    token = _current.set(end)
    try:
        remaining(float('inf'))
        yield
    finally:
        _current.reset(token)
