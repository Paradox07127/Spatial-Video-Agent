import httpx
import pytest

from app.core import llm


@pytest.mark.parametrize('env,auth', [({'LLM_API_KEY': 'k', 'GPU_BEARER_TOKEN': 'g'}, 'Bearer k'),
                                      ({'GPU_BEARER_TOKEN': 'g'}, None)])
def test_complete_json_auth(monkeypatch, env, auth):
    monkeypatch.setenv('LLM_URL', 'http://llm/v1')
    monkeypatch.delenv('LLM_API_KEY', raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"ok": 1}'}}]})
    client = httpx.Client
    monkeypatch.setattr(llm.httpx, 'Client', lambda **kw: client(transport=httpx.MockTransport(handler), **kw))
    assert llm.complete_json([{'role': 'user', 'content': 'q'}], role='parse') == {'ok': 1}
    assert seen[0].headers.get('Authorization') == auth
