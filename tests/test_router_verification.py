# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import copy
import json
import subprocess
import sys

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from router_control import app as module
from router_control.verification import MAX_JSON_DEPTH, MAX_VERIFICATION_BYTES, sha256

client = TestClient(module.app)


def _direct_http_request() -> Request:
    return Request({
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "headers": [(b"authorization", b"Bearer fixture-caller")],
        "client": ("127.0.0.1", 41000),
        "server": ("router.example.test", 443),
    })


def _configure_timeout_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SZL_ROUTER_ENABLE_EGRESS", "1")
    monkeypatch.setenv("SZL_ROUTER_TOKEN", "fixture-caller")
    monkeypatch.setenv("FIXTURE_TOKEN", "fixture-provider")
    monkeypatch.setenv(
        "SZL_ROUTER_ALLOWED_HOSTS",
        "slow.example.test,fallback.example.test",
    )
    monkeypatch.setenv("SZL_ROUTER_PROVIDERS_JSON", json.dumps({"providers": [
        {
            "id": "slow",
            "base_url": "https://slow.example.test/v1",
            "models": {"szl-default": "fixture-model"},
            "token_env": "FIXTURE_TOKEN",
            "priority": 0,
            "enabled": True,
        },
        {
            "id": "fallback",
            "base_url": "https://fallback.example.test/v1",
            "models": {"szl-default": "fixture-model"},
            "token_env": "FIXTURE_TOKEN",
            "priority": 1,
            "enabled": True,
        },
    ]}))


@pytest.fixture
def bundle(monkeypatch):
    monkeypatch.setenv("SZL_ROUTER_ENABLE_EGRESS", "1")
    monkeypatch.setenv("SZL_ROUTER_TOKEN", "fixture-caller")
    monkeypatch.setenv("FIXTURE_TOKEN", "fixture-provider")
    monkeypatch.setenv("SZL_ROUTER_ALLOWED_HOSTS", "fixture.example.test")
    monkeypatch.setenv("SZL_ROUTER_PROVIDERS_JSON", json.dumps({"providers": [{
        "id": "fixture", "base_url": "https://fixture.example.test/v1",
        "models": {"szl-default": "fixture-model"}, "token_env": "FIXTURE_TOKEN",
        "enabled": True}]}))

    async def fake_call(*args):
        return {"id": "completion-fixture", "model": "fixture-model", "choices": [
            {"message": {"role": "assistant", "content": "Unicode: λ 🌱"}}]}, 200

    monkeypatch.setattr(module, "call_provider", fake_call)
    request = {"model": "szl-default", "messages": [{"role": "user", "content": "Verify λ"}]}
    response = client.post("/v1/chat/completions", json=request,
                           headers={"Authorization": "Bearer fixture-caller"})
    assert response.status_code == 200
    return {"completion": response.json(), "request": request}


def test_emitted_receipt_verifies_without_egress_or_caller_token(bundle, monkeypatch):
    before = copy.deepcopy(bundle)
    monkeypatch.delenv("SZL_ROUTER_TOKEN")
    monkeypatch.setenv("SZL_ROUTER_ENABLE_EGRESS", "0")
    response = client.post("/api/verify", json=bundle,
                           headers={"X-SZL-Receipt": bundle["completion"]["szl_receipt"]["digest"]})
    assert response.status_code == 200
    assert response.json() == {"schema": "szl.router-verification/v1", "status": "CONSISTENT",
                               "checks": {"receipt_digest": True, "request_digest": True,
                                          "response_digest": True, "header_digest": True},
                               "trust": "UNSIGNED_HONEST", "identity_verified": False}
    assert response.headers["cache-control"] == "no-store"
    assert bundle == before


@pytest.mark.parametrize("target,check", [("answer", "response_digest"),
                                          ("request", "request_digest"),
                                          ("receipt", "receipt_digest")])
def test_independent_tampering_is_divergent(bundle, target, check):
    if target == "answer":
        bundle["completion"]["choices"][0]["message"]["content"] = "modified"
    elif target == "request":
        bundle["request"]["messages"][0]["content"] = "modified"
    else:
        bundle["completion"]["szl_receipt"]["provider_id"] = "modified"
    response = client.post("/api/verify", json=bundle)
    assert response.status_code == 422
    assert response.json()["status"] == "DIVERGENT"
    assert response.json()["checks"][check] is False


def test_wrong_header_and_missing_receipt_are_divergent(bundle):
    response = client.post("/api/verify", json=bundle, headers={"X-SZL-Receipt": "f" * 64})
    assert response.status_code == 422
    assert response.json()["checks"]["header_digest"] is False
    del bundle["completion"]["szl_receipt"]
    assert not any(client.post("/api/verify", json=bundle).json()["checks"].values())


def test_rehashing_tampered_bundle_does_not_establish_identity(bundle):
    completion = bundle["completion"]
    completion["choices"][0]["message"]["content"] = "manufactured answer"
    receipt = completion["szl_receipt"]
    receipt["response_digest"] = sha256({key: value for key, value in completion.items() if key != "szl_receipt"})
    receipt["digest"] = sha256({key: value for key, value in receipt.items() if key not in {"digest", "algorithm"}})
    result = client.post("/api/verify", json=bundle).json()
    assert result["status"] == "CONSISTENT"
    assert result["identity_verified"] is False
    assert result["trust"] == "UNSIGNED_HONEST"


@pytest.mark.parametrize("raw", [b"not JSON", b'{"completion":{},"request":{},"request":{}}',
                                    b'{"completion":{"x":NaN},"request":{}}',
                                    b'{"completion":[],"request":{}}',
                                    b'{"completion":{},"request":{"secret":"never-echo"}}'])
def test_malformed_input_returns_sanitized_error(raw):
    response = client.post("/api/verify", content=raw)
    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "INVALID_VERIFICATION_INPUT"}}
    assert "never-echo" not in response.text


def test_byte_depth_and_completion_bounds(bundle):
    assert client.post("/api/verify", content=b" " * (MAX_VERIFICATION_BYTES + 1)).status_code == 413
    nested = {"x": None}
    for _ in range(MAX_JSON_DEPTH + 1):
        nested = {"x": nested}
    bundle["completion"]["nested"] = nested
    assert client.post("/api/verify", json=bundle).json()["detail"]["code"] == "INVALID_VERIFICATION_INPUT"
    del bundle["completion"]["nested"]
    bundle["completion"]["oversize"] = "x" * 2_032_769
    assert client.post("/api/verify", json=bundle).json()["detail"]["code"] == "INVALID_VERIFICATION_INPUT"


def test_cli_supports_file_stdin_and_divergent_exit_codes(bundle, tmp_path):
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    command = [sys.executable, "-m", "router_control.verification"]
    file_result = subprocess.run([*command, str(path)], capture_output=True, cwd=module.ROOT)
    stdin_result = subprocess.run([*command, "-"], input=path.read_bytes(), capture_output=True, cwd=module.ROOT)
    assert file_result.returncode == stdin_result.returncode == 0
    assert json.loads(file_result.stdout) == json.loads(stdin_result.stdout)
    bundle["request"]["messages"][0]["content"] = "tampered"
    divergent = subprocess.run([*command, "-"], input=json.dumps(bundle).encode(), capture_output=True, cwd=module.ROOT)
    invalid = subprocess.run([*command, "-"], input=b"invalid", capture_output=True, cwd=module.ROOT)
    assert divergent.returncode == 1
    assert invalid.returncode == 2
    assert json.loads(divergent.stdout)["status"] == "DIVERGENT"


def test_reserved_upstream_receipt_field_is_rejected():
    with pytest.raises(RuntimeError, match="reserved receipt"):
        module.validate_completion({"szl_receipt": {}, "choices": [{"message": {"role": "assistant", "content": "ok"}}]})


@pytest.mark.parametrize("overflow", ["depth", "nodes", "completion_bytes"])
def test_gateway_never_emits_success_outside_verifier_bounds(monkeypatch, overflow):
    monkeypatch.setenv("SZL_ROUTER_ENABLE_EGRESS", "1")
    monkeypatch.setenv("SZL_ROUTER_TOKEN", "fixture-caller")
    monkeypatch.setenv("FIXTURE_TOKEN", "fixture-provider")
    monkeypatch.setenv("SZL_ROUTER_ALLOWED_HOSTS", "fixture.example.test")
    monkeypatch.setenv("SZL_ROUTER_PROVIDERS_JSON", json.dumps({"providers": [{
        "id": name, "base_url": "https://fixture.example.test/v1",
        "models": {"szl-default": "fixture-model"}, "token_env": "FIXTURE_TOKEN",
        "priority": priority, "enabled": True,
    } for priority, name in enumerate(("overbound", "fallback"))]}))
    extra = None
    if overflow == "depth":
        for _ in range(70):
            extra = {"x": extra}
    elif overflow == "nodes":
        extra = [0] * 100_001
    else:
        extra = "x" * 2_032_769
    calls = []

    async def fake_call(provider, payload):
        calls.append(provider.id)
        completion = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        if provider.id == "overbound":
            completion["extra"] = extra
        return completion, 200

    monkeypatch.setattr(module, "call_provider", fake_call)
    request = {"model": "szl-default", "messages": [{"role": "user", "content": "Verify bounds"}]}
    response = client.post("/v1/chat/completions", json=request,
                           headers={"Authorization": "Bearer fixture-caller"})
    assert response.status_code == 200
    assert calls == ["overbound", "fallback"]
    completion = response.json()
    assert completion["szl_receipt"]["provider_id"] == "fallback"
    assert [attempt["state"] for attempt in completion["szl_receipt"]["attempts"]] == [
        "TRANSPORT_OR_CONTRACT_ERROR", "SUCCESS",
    ]
    verified = client.post("/api/verify", json={"completion": completion, "request": request})
    assert verified.status_code == 200
    assert verified.json()["status"] == "CONSISTENT"


def test_whole_provider_attempt_deadline_stops_ambiguous_failover(monkeypatch):
    monkeypatch.setenv("SZL_ROUTER_ENABLE_EGRESS", "1")
    monkeypatch.setenv("SZL_ROUTER_TOKEN", "fixture-caller")
    monkeypatch.setenv("FIXTURE_TOKEN", "fixture-provider")
    monkeypatch.setenv("SZL_ROUTER_ALLOWED_HOSTS", "fixture.example.test")
    monkeypatch.setenv("SZL_ROUTER_PROVIDERS_JSON", json.dumps({"providers": [{
        "id": name, "base_url": "https://fixture.example.test/v1",
        "models": {"szl-default": "fixture-model"}, "token_env": "FIXTURE_TOKEN",
        "priority": priority, "enabled": True,
    } for priority, name in enumerate(("slow", "fallback"))]}))
    monkeypatch.setattr(module, "MAX_TIMEOUT_SECONDS", 0.01)
    calls = []
    slow_cancelled = []

    async def fake_call(provider, payload):
        calls.append(provider.id)
        if provider.id == "slow":
            try:
                await asyncio.sleep(0.05)
            finally:
                slow_cancelled.append(True)
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}, 200

    monkeypatch.setattr(module, "call_provider", fake_call)
    request = {"model": "szl-default", "messages": [{"role": "user", "content": "Verify deadline"}]}
    response = client.post("/v1/chat/completions", json=request,
                           headers={"Authorization": "Bearer fixture-caller"})
    assert response.status_code == 504
    assert calls == ["slow"]
    assert slow_cancelled == [True]
    failure = response.json()["detail"]
    assert failure["state"] == "ATTEMPT_OUTCOME_UNKNOWN"
    assert failure["attempts"] == [{
        "provider_id": "slow",
        "state": "UPSTREAM_DEADLINE_EXCEEDED",
        "outcome": "UNKNOWN",
    }]
    assert failure["automatic_retry_safe"] is False
    assert failure["reconciliation"] == "REQUIRED_BEFORE_RETRY"
    assert failure["secret_material_recorded"] is False
    assert len(failure["digest"]) == 64


@pytest.mark.parametrize("timeout_error", [
    asyncio.TimeoutError(),
    httpx.ReadTimeout("provider response deadline expired"),
])
def test_timeout_variants_are_receipted_without_fallback(
    monkeypatch: pytest.MonkeyPatch, timeout_error: Exception,
) -> None:
    """Exercise both wall-timer and httpx deadlines without a Windows event loop."""
    _configure_timeout_pair(monkeypatch)
    calls: list[str] = []

    async def timeout_then_forbidden(provider, request, upstream_model):
        del request, upstream_model
        calls.append(provider.id)
        if provider.id == "slow":
            raise timeout_error
        raise AssertionError("an ambiguous timeout reached the fallback provider")

    async def immediate_wait(awaitable, timeout):
        # The audit sandbox cannot create asyncio's Windows self-pipe. Hosted CI
        # retains the integration test above for the real wait_for deadline.
        del timeout
        return await awaitable

    monkeypatch.setattr(module, "attempt_candidate", timeout_then_forbidden)
    monkeypatch.setattr(module.asyncio, "wait_for", immediate_wait)
    request = module.ChatRequest.model_validate({
        "model": "szl-default",
        "messages": [{"role": "user", "content": "one logical request"}],
    })
    operation = module.chat(request, _direct_http_request())
    with pytest.raises(HTTPException) as caught:
        operation.send(None)

    assert caught.value.status_code == 504
    detail = caught.value.detail
    assert calls == ["slow"]
    assert detail["state"] == "ATTEMPT_OUTCOME_UNKNOWN"
    assert detail["attempts"] == [{
        "provider_id": "slow",
        "state": "UPSTREAM_DEADLINE_EXCEEDED",
        "outcome": "UNKNOWN",
    }]
    assert detail["automatic_retry_safe"] is False
    assert detail["reconciliation"] == "REQUIRED_BEFORE_RETRY"
    assert detail["secret_material_recorded"] is False
    assert detail["digest"] == module.sha256({
        key: value for key, value in detail.items() if key != "digest"
    })
    serialized = json.dumps(detail)
    assert "fixture-provider" not in serialized
    assert "slow.example.test" not in serialized
    assert "fallback.example.test" not in serialized


def test_version_and_source_controlled_verifier_are_honest(monkeypatch):
    monkeypatch.delenv("SOURCE_REVISION", raising=False)
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    monkeypatch.setenv("SPACE_COMMIT_SHA", "a" * 40)
    assert client.get("/version").json()["git_sha"] == "UNAVAILABLE"
    monkeypatch.setenv("SOURCE_REVISION", "b" * 40)
    payload = client.get("/version").json()
    assert payload == {"version": module.APP_VERSION, "git_sha": "b" * 40, "model_sha": None,
                       "model_sha_state": "UNAVAILABLE_MUTABLE_MODEL_ALIASES"}
    assert "router_control/verification.py" in client.get("/api/source").json()["controlled_files"]
    assert "router_control/local_store.py" in client.get("/api/source").json()["controlled_files"]
