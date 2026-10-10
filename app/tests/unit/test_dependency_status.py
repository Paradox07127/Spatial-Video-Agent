from types import SimpleNamespace
from app.api.health import dependency_status


def test_dependency_failures_are_separate_and_do_not_expose_urls(monkeypatch):
    def offline(*args, **kwargs):
        raise ConnectionRefusedError('private endpoint details')
    monkeypatch.setattr('app.api.health.socket.create_connection', offline)
    out = dependency_status(SimpleNamespace(vlm_url='http://secret-host:8101/v1', embed_url='http://secret-host:8103'))
    assert out['degraded'] is True and out['inference_verified'] is False
    assert out['services']['vlm']['status'] == 'unavailable'
    assert out['services']['llm']['status'] == 'not_configured'
    assert 'secret-host' not in str(out) and 'private' not in str(out)


def test_dependency_reachability_does_not_claim_inference_verified(monkeypatch):
    class Connected:
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr('app.api.health.socket.create_connection', lambda *a, **kw: Connected())
    out = dependency_status(SimpleNamespace(vlm_url='https://host/v1'))
    assert out['services']['vlm']['status'] == 'reachable'
    assert not out['degraded'] and not out['inference_verified']
