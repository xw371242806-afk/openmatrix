import pytest
import httpx
from openai import OpenAI
from core.model_provider import model_request_headers


def test_go_headers_use_honest_agent_and_supplied_session():
    headers = model_request_headers("https://opencode.ai/zen/go/v1/", "trial-session")
    assert headers == {"User-Agent": "OpenMatrix/1.0", "x-opencode-session": "trial-session"}


@pytest.mark.parametrize("url", [
    "https://example.com/v1", "https://opencode.ai.evil.example/zen/go/v1",
    "http://opencode.ai/zen/go/v1", "https://opencode.ai/zen/v1",
    "https://opencode.ai:444/zen/go/v1", "https://user:password@opencode.ai/zen/go/v1",
    "https://opencode.ai:invalid/zen/go/v1",
])
def test_go_headers_do_not_change_other_endpoints(url):
    assert model_request_headers(url) == {}


def test_go_session_stays_stable_across_sdk_calls_and_retries():
    seen = []
    def handler(request):
        seen.append(dict(request.headers))
        if len(seen) == 1:
            return httpx.Response(500, json={"error": "fixture retry"})
        return httpx.Response(200, json={"id": "fixture", "object": "chat.completion", "created": 0, "model": "deepseek-v4-flash", "choices": [{"index": 0, "message": {"role": "assistant", "content": "READY"}, "finish_reason": "stop"}]})
    headers = model_request_headers("https://opencode.ai/zen/go/v1")
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = OpenAI(api_key="fixture-only", base_url="https://opencode.ai/zen/go/v1", default_headers=headers, http_client=http, max_retries=1)
        for _ in range(2):
            assert client.chat.completions.create(model="deepseek-v4-flash", messages=[{"role": "user", "content": "fixture"}]).choices[0].message.content == "READY"
    assert len(seen) == 3
    assert {h["x-opencode-session"] for h in seen} == {headers["x-opencode-session"]}
    assert all(h["user-agent"] == "OpenMatrix/1.0" for h in seen)


@pytest.mark.parametrize("base_url", ["https://api.deepseek.com", "https://api.deepseek.com/v1"])
def test_native_deepseek_openai_protocol_and_model_discovery(base_url):
    seen = []
    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": [{"id": "deepseek-flash", "object": "model", "created": 0, "owned_by": "deepseek"}]})
        return httpx.Response(200, json={"id": "fixture", "object": "chat.completion", "created": 0, "model": "deepseek-flash", "choices": [{"index": 0, "message": {"role": "assistant", "content": "READY"}, "finish_reason": "stop"}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = OpenAI(api_key="fixture-only", base_url=base_url, default_headers=model_request_headers(base_url), http_client=http)
        assert [m.id for m in client.models.list().data] == ["deepseek-flash"]
        assert client.chat.completions.create(model="deepseek-flash", messages=[{"role": "user", "content": "fixture"}]).model == "deepseek-flash"
    assert len(seen) == 2
    assert all(r.headers["authorization"] == "Bearer fixture-only" for r in seen)
    assert all("x-opencode-session" not in r.headers and "api_key" not in str(r.url) for r in seen)
    assert seen[1].url.path == ("/v1/chat/completions" if base_url.endswith("/v1") else "/chat/completions")


@pytest.mark.parametrize("fail", [False, True])
def test_model_discovery_closes_transport_on_success_and_failure(monkeypatch, fail):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    import main
    transports = []
    real_transport_factory = httpx.AsyncClient
    def safe_factory(**kwargs):
        http = real_transport_factory(**kwargs)
        transports.append(http)
        return http
    async def list_models():
        if fail:
            raise RuntimeError("fixture-only rejected")
        return SimpleNamespace(data=[SimpleNamespace(id="deepseek-flash")])
    monkeypatch.setattr(main.httpx, "AsyncClient", safe_factory)
    monkeypatch.setattr(main, "AsyncOpenAI", lambda **kwargs: SimpleNamespace(models=SimpleNamespace(list=list_models)))
    monkeypatch.setattr(main.TenacityRetryAdapter, "decorate", lambda self, fn: fn)
    with TestClient(main.app) as app:
        response = app.post("/api/models", json={"base_url": "https://api.deepseek.com", "api_key": "fixture-only"})
    assert response.status_code == (500 if fail else 200)
    assert len(transports) == 1 and transports[0].is_closed
    assert "fixture-only" not in response.text
