"""Unknown or mistyped model names return an OpenAI-shaped 404 before any upstream call."""
import asyncio

import httpx
import pytest

from szl_router import app as app_mod

MSG = [{"role": "user", "content": "hi"}]


def _post(path, body):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_mod.app),
                                     base_url="http://t") as c:
            return await c.post(path, json=body)
    return asyncio.run(go())


@pytest.fixture
def no_upstream(monkeypatch):
    calls = []
    ok = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
          "x_szl_provenance": {}}
    monkeypatch.setattr(app_mod.core, "chat", lambda *a, **k: calls.append(("chat", a)) or ok)
    monkeypatch.setattr(app_mod.core, "embed",
                        lambda *a, **k: calls.append(("embed", a)) or {"data": []})
    monkeypatch.setattr(app_mod.receipts, "build_envelope", lambda **kw: None)
    monkeypatch.delenv("SZL_ROUTER_TOKEN", raising=False)
    return calls


@pytest.mark.parametrize("model", [
    "totally-unknown",
    "szl\u2010fast",          # Unicode hyphen look-alike
    "SZL-FAST",               # case is not folded
    "nosuchprovider:llama3",  # unknown provider prefix
    "box_gpu:",               # empty upstream model
    "bge-large",              # embeddings model on the chat route
])
def test_chat_unknown_model_404(no_upstream, model):
    r = _post("/v1/chat/completions", {"model": model, "messages": MSG})
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["code"] == "model_not_found" and err["param"] == "model"
    assert no_upstream == []


@pytest.mark.parametrize("model", ["szl-large", "szl-fast", "szl-coder", "szl-auto", "box_gpu:llama3.1:8b"])
def test_chat_known_models_still_route(no_upstream, model):
    r = _post("/v1/chat/completions", {"model": model, "messages": MSG})
    assert r.status_code == 200
    assert no_upstream and no_upstream[0][1][0] == model


def test_chat_non_string_model_400(no_upstream):
    r = _post("/v1/chat/completions", {"model": 7, "messages": MSG})
    assert r.status_code == 400
    assert no_upstream == []


def test_chat_missing_model_uses_default(no_upstream):
    r = _post("/v1/chat/completions", {"messages": MSG})
    assert r.status_code == 200
    assert no_upstream[0][1][0] == app_mod.core.DEFAULT_MODEL


@pytest.mark.parametrize("model", ["bge-lrage", "szl-large", "nosuch:bge"])
def test_embeddings_unknown_model_404(no_upstream, model):
    r = _post("/v1/embeddings", {"model": model, "input": "x"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "model_not_found"
    assert no_upstream == []


def test_embeddings_known_model_routes(no_upstream):
    r = _post("/v1/embeddings", {"model": "bge-large", "input": "x"})
    assert r.status_code == 200
