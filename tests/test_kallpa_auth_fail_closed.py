"""Auth fails closed without a token; base_url is redacted for non-public hosts."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import szl_router.app as app_mod
from szl_router import core


@pytest.fixture()
def client():
    with TestClient(app_mod.app) as c:
        yield c


@pytest.mark.parametrize("path,body", [
    ("/v1/chat/completions", {"model": "szl-fast", "messages": [{"role": "user", "content": "hi"}]}),
    ("/v1/embeddings", {"model": "bge-large", "input": "hi"}),
])
def test_no_token_fails_closed(client, monkeypatch, path, body):
    monkeypatch.delenv("SZL_ROUTER_TOKEN", raising=False)
    monkeypatch.delenv("SZL_ROUTER_ALLOW_NO_AUTH", raising=False)
    r = client.post(path, json=body)
    assert r.status_code == 503
    assert "SZL_ROUTER_TOKEN" in r.json()["detail"]


def test_allow_flag_must_be_exactly_1(client, monkeypatch):
    monkeypatch.delenv("SZL_ROUTER_TOKEN", raising=False)
    for v in ("true", "yes", "0", ""):
        monkeypatch.setenv("SZL_ROUTER_ALLOW_NO_AUTH", v)
        r = client.post("/v1/embeddings", json={"input": "x"})
        assert r.status_code == 503, v


def test_healthz_stays_open_without_token(client, monkeypatch):
    monkeypatch.delenv("SZL_ROUTER_TOKEN", raising=False)
    monkeypatch.delenv("SZL_ROUTER_ALLOW_NO_AUTH", raising=False)
    assert client.get("/healthz").status_code == 200


def test_token_set_requires_bearer(client, monkeypatch):
    monkeypatch.setenv("SZL_ROUTER_TOKEN", "synthetic-token")
    monkeypatch.setenv("SZL_ROUTER_ALLOW_NO_AUTH", "1")  # flag never overrides a set token
    r = client.post("/v1/embeddings", json={"input": "x"})
    assert r.status_code == 401
    r = client.post("/v1/embeddings", json={"input": "x"},
                    headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


@pytest.mark.parametrize("url,expected", [
    ("https://api.groq.com/openai/v1", "https://api.groq.com/openai/v1"),
    ("http://api.groq.com/openai/v1", "redacted"),
    ("https://user:pw@api.groq.com/openai/v1", "redacted"),
    ("http://100.101.102.103:11434/v1", "redacted"),
    ("http://127.0.0.1:11434/v1", "redacted"),
    ("https://gpu-node.example.internal/v1", "redacted"),
    (None, None),
])
def test_redact_base_url(monkeypatch, url, expected):
    monkeypatch.delenv("SZL_ROUTER_EXPOSE_BASE_URL", raising=False)
    assert app_mod._redact_base_url(url) == expected


def test_expose_flag_restores_url(monkeypatch):
    monkeypatch.setenv("SZL_ROUTER_EXPOSE_BASE_URL", "1")
    assert app_mod._redact_base_url("http://10.0.0.5:11434/v1") == "http://10.0.0.5:11434/v1"


def test_status_redacts_configured_node(client, monkeypatch):
    monkeypatch.delenv("SZL_ROUTER_EXPOSE_BASE_URL", raising=False)
    monkeypatch.setenv("OMEN_GPU_BASE_URL", "http://100.64.1.2:11434/v1")
    rows = {r["provider"]: r for r in client.get("/status").json()["providers"]}
    assert rows["omen_gpu"]["base_url"] == "redacted"
    assert "100.64.1.2" not in client.get("/status").text


def test_chat_response_redacts_served_node(client, monkeypatch):
    monkeypatch.delenv("SZL_ROUTER_TOKEN", raising=False)
    monkeypatch.setenv("SZL_ROUTER_ALLOW_NO_AUTH", "1")
    monkeypatch.delenv("SZL_ROUTER_EXPOSE_BASE_URL", raising=False)

    def fake_chat(model, messages, **kw):
        return {"choices": [], "x_szl_provenance": {
            "served_by": "omen_gpu:llama3.1:8b", "base_url": "http://100.64.1.2:11434/v1",
            "sovereign": True, "tier": "sovereign", "attempts": []}}

    monkeypatch.setattr(core, "chat", fake_chat)
    r = client.post("/v1/chat/completions",
                    json={"model": "szl-fast", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert r.json()["x_szl_provenance"]["base_url"] == "redacted"
    assert "100.64.1.2" not in r.text
