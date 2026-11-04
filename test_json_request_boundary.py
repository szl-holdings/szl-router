"""Offline HTTP boundary tests: invalid JSON bodies never reach dependencies."""
import pytest
from types import SimpleNamespace
from fastapi.testclient import TestClient

from szl_router import app as gateway


ENDPOINTS = ("/v1/chat/completions", "/v1/embeddings", "/v1/receipt/verify")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("SZL_ROUTER_TOKEN", raising=False)
    # No lifespan/startup, signing key initialization, provider or network call.
    return TestClient(gateway.app, raise_server_exceptions=False)


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("body", [
    b"[]", b"null", b"true", b"7", b'"synthetic text"',
    b'{"broken":', b'{"value":"\xff"}',
    b'{"messages":[{}],"input":"hello","envelope":{},"extra":NaN}',
    b'{"messages":[{}],"input":"hello","envelope":{},"extra":Infinity}',
    b'{"messages":[{}],"input":"hello","envelope":{},"extra":-Infinity}',
    b'{"messages":[{}],"input":"hello","envelope":{},"extra":1e999}',
    b'{"messages":[{}],"input":"hello","envelope":{},"extra":-1e999}',
    b'{"nested":' * 10000 + b'0' + b'}' * 10000,
])
def test_invalid_body_returns_client_error_before_dependency_call(client, monkeypatch, endpoint, body):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("invalid request crossed the HTTP boundary")

    monkeypatch.setattr(gateway.core, "chat", forbidden)
    monkeypatch.setattr(gateway.core, "embed", forbidden)
    monkeypatch.setattr(gateway.receipts, "verify_envelope", forbidden)
    response = client.post(endpoint, content=body,
                           headers={"content-type": "application/json"})
    assert response.status_code == 400
    assert isinstance(response.json()["detail"], str)
    assert "synthetic text" not in response.text
    assert calls == []


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_empty_object_keeps_endpoint_required_field_error(client, endpoint):
    response = client.post(endpoint, json={})
    assert response.status_code == 400
    assert response.json()["detail"].endswith("is required")


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_parser_recursion_failure_is_a_controlled_client_error(client, monkeypatch, endpoint):
    def reject_depth(*args, **kwargs):
        raise RecursionError("synthetic decoder limit")

    monkeypatch.setattr(gateway, "json", SimpleNamespace(loads=reject_depth))
    response = client.post(endpoint, content=b"{}",
                           headers={"content-type": "application/json"})
    assert response.status_code == 400
    assert "synthetic decoder limit" not in response.text


@pytest.mark.parametrize("endpoint", ENDPOINTS[:2])
def test_authentication_still_precedes_json_parsing(client, monkeypatch, endpoint):
    monkeypatch.setenv("SZL_ROUTER_TOKEN", "synthetic-token")
    response = client.post(endpoint, content=b'{"broken":',
                           headers={"content-type": "application/json"})
    assert response.status_code == 401


def test_valid_chat_object_preserves_routing_and_response(client, monkeypatch):
    calls = []

    def chat(model, messages, **kwargs):
        calls.append((model, messages, kwargs))
        return {"choices": [{"message": {"role": "assistant", "content": "synthetic"}}]}

    monkeypatch.setattr(gateway.core, "chat", chat)
    monkeypatch.setattr(gateway.grid, "enabled", lambda: False)
    monkeypatch.setattr(gateway.receipts, "build_envelope", lambda **kwargs: None)
    messages = [{"role": "user", "content": "hello"}]
    response = client.post(ENDPOINTS[0], content=(
        b'{"model":"szl-fast","messages":[{"role":"user","content":"hello"}],'
        b'"temperature":1.25e-1}'
    ), headers={"content-type": "application/json"})
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "synthetic"
    assert calls[0][:2] == ("szl-fast", messages)
    assert calls[0][2]["temperature"] == 0.125


def test_valid_embeddings_object_preserves_routing_and_response(client, monkeypatch):
    calls = []

    def embed(model, input_, **kwargs):
        calls.append((model, input_, kwargs))
        return {"data": [{"index": 0, "embedding": [0.25, 0.5]}]}

    monkeypatch.setattr(gateway.core, "embed", embed)
    response = client.post(ENDPOINTS[1], json={"input": ["hello"], "dimensions": 2})
    assert response.status_code == 200
    assert response.json()["data"][0]["embedding"] == [0.25, 0.5]
    assert calls == [("bge-large", ["hello"], {"extra": {"dimensions": 2}})]


def test_valid_receipt_object_preserves_public_verifier_contract(client, monkeypatch):
    calls = []

    def verify(envelope, key):
        calls.append((envelope, key))
        return False, "unsigned-honest"

    monkeypatch.setenv("SZL_ROUTER_TOKEN", "synthetic-token")
    monkeypatch.setattr(gateway.receipts, "verify_envelope", verify)
    response = client.post(ENDPOINTS[2], json={"envelope": {"signed": False}})
    assert response.status_code == 200
    assert response.json() == {"valid": False, "detail": "unsigned-honest", "signed": False}
    assert calls == [({"signed": False}, None)]
