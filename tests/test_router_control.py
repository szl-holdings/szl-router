# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import asyncio
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from router_control import app as module

client = TestClient(module.app, headers={"Authorization": "Bearer test-router-client"})
CONFIG_VARS = (
    "SZL_ROUTER_ALLOWED_HOSTS",
    "SZL_ROUTER_PROVIDERS_JSON",
    "SZL_ROUTER_OLLAMA_MODELS_DIR",
    "SZL_ROUTER_ENABLE_EGRESS",
    "SZL_ROUTER_TOKEN",
    "SOURCE_REVISION",
    "GIT_COMMIT",
    "SPACE_COMMIT_SHA",
    "SOVEREIGN_TOKEN",
    "REGIONAL_TOKEN",
    "REMOTE_TOKEN",
)

LOCAL_DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64
LOCAL_UPSTREAM = "a11oy-mini-r2:latest"


def local_registry_payload(*, with_remote: bool = False) -> dict[str, Any]:
    providers: list[dict[str, Any]] = [{
        "id": "local-ollama",
        "provider_type": "ollama_loopback",
        "models": {"a11oy-mini-r2": LOCAL_UPSTREAM},
        "model_digests": {"a11oy-mini-r2": LOCAL_DIGEST},
        "sovereignty": 100,
        "classifications": ["public", "internal"],
        "enabled": True,
    }]
    if with_remote:
        providers.append({
            "id": "remote",
            "base_url": "https://remote.example.test/v1",
            "models": {"a11oy-mini-r2": "remote-model"},
            "token_env": "REMOTE_TOKEN",
            "sovereignty": 10,
            "classifications": ["public"],
            "enabled": True,
        })
    return {"providers": providers}


def configure_local(monkeypatch: pytest.MonkeyPatch, *, egress: bool = True,
                    with_remote: bool = False) -> None:
    monkeypatch.setenv("SZL_ROUTER_PROVIDERS_JSON", json.dumps(local_registry_payload(with_remote=with_remote)))
    monkeypatch.setenv("SZL_ROUTER_ALLOWED_HOSTS", "remote.example.test" if with_remote else "")
    monkeypatch.setenv("SZL_ROUTER_ENABLE_EGRESS", "1" if egress else "0")
    monkeypatch.setenv("SZL_ROUTER_TOKEN", "test-router-client")
    if with_remote:
        monkeypatch.setenv("REMOTE_TOKEN", "remote-test-secret")


@pytest.fixture(autouse=True)
def clean_router_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    module._local_probe_cache = None
    for name in CONFIG_VARS:
        monkeypatch.delenv(name, raising=False)


def registry_payload() -> dict[str, Any]:
    return {
        "providers": [
            {
                "id": "regional",
                "base_url": "https://regional.example.test/v1",
                "models": {"szl-default": "regional-model"},
                "token_env": "REGIONAL_TOKEN",
                "priority": 10,
                "sovereignty": 70,
                "cost_tier": 2,
                "classifications": ["public", "internal"],
                "enabled": True,
            },
            {
                "id": "sovereign",
                "base_url": "https://sovereign.example.test/v1",
                "models": {"szl-default": "sovereign-model"},
                "token_env": "SOVEREIGN_TOKEN",
                "priority": 40,
                "sovereignty": 95,
                "cost_tier": 4,
                "classifications": ["public", "internal", "confidential"],
                "enabled": True,
            },
        ]
    }


def configure(monkeypatch: pytest.MonkeyPatch, *, egress: bool = False, tokens: bool = False) -> None:
    monkeypatch.setenv(
        "SZL_ROUTER_ALLOWED_HOSTS",
        "regional.example.test,sovereign.example.test",
    )
    monkeypatch.setenv(
        "SZL_ROUTER_PROVIDERS_JSON",
        json.dumps(registry_payload()),
    )
    monkeypatch.setenv("SZL_ROUTER_ENABLE_EGRESS", "1" if egress else "0")
    monkeypatch.setenv("SZL_ROUTER_TOKEN", "test-router-client")
    if tokens:
        monkeypatch.setenv("REGIONAL_TOKEN", "regional-test-secret")
        monkeypatch.setenv("SOVEREIGN_TOKEN", "sovereign-test-secret")


def chat_request(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "model": "szl-default",
        "messages": [{"role": "user", "content": "Return a bounded status."}],
        "stream": False,
        "data_classification": "public",
        "max_cost_tier": 10,
    }
    value.update(overrides)
    return value


def test_unconfigured_router_is_ready_but_egress_disabled() -> None:
    response = client.get("/readyz")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["registry_state"] == "UNAVAILABLE_NOT_CONFIGURED"
    assert payload["egress_enabled"] is False


def test_source_identity_exposes_no_secret_or_arbitrary_url_authority() -> None:
    payload = client.get("/api/source").json()
    assert payload["repository"] == "szl-holdings/szl-router"
    assert payload["default_egress"] is False
    assert payload["secret_output"] is False
    assert payload["arbitrary_url_routing"] is False
    assert len(payload["receipt"]["digest"]) == 64


def test_invalid_or_local_provider_configuration_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SZL_ROUTER_ALLOWED_HOSTS", "localhost")
    payload = registry_payload()
    payload["providers"] = [
        {
            **payload["providers"][0],
            "base_url": "https://localhost/v1",
        }
    ]
    monkeypatch.setenv("SZL_ROUTER_PROVIDERS_JSON", json.dumps(payload))
    monkeypatch.setenv("SZL_ROUTER_ENABLE_EGRESS", "1")
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["registry_state"] == "INVALID_FAIL_CLOSED"
    routes = client.get("/api/routes").json()
    assert routes["state"] == "INVALID_FAIL_CLOSED"
    assert routes["providers"] == []


def test_literal_ip_and_non_https_provider_urls_are_rejected() -> None:
    with pytest.raises(ValueError, match="literal IP"):
        module.validate_base_url("https://127.0.0.1/v1", frozenset({"127.0.0.1"}))
    with pytest.raises(ValueError, match="must use https"):
        module.validate_base_url("http://provider.example.test/v1", frozenset({"provider.example.test"}))


@pytest.mark.parametrize("upstream", [
    "Pro/zai-org/GLM-4.5",
    "Qwen/Qwen3-8B:free",
    "deepseek-ai/DeepSeek-V3.1",
])
def test_https_provider_accepts_bounded_qualified_model_names(upstream: str) -> None:
    provider = module.ProviderRecord.model_validate({
        "id": "remote",
        "base_url": "https://remote.example.test/v1",
        "models": {"szl-default": upstream},
        "token_env": "REMOTE_TOKEN",
        "enabled": False,
    })
    assert provider.models["szl-default"] == upstream
    assert provider.enabled is False


@pytest.mark.parametrize("upstream", [
    "/model", "org/../model", "org//model", "org/model/", "https://example.test/model",
    "https:/example.test/model", "org/model?key=x", "org/model#fragment", "org/%2e%2e/model",
    "org\\model", "org/model\nheader: value", "org/" + "a" * 190,
])
def test_https_provider_rejects_unsafe_model_names(upstream: str) -> None:
    with pytest.raises(ValueError, match="bounded upstream model names"):
        module.ProviderRecord.model_validate({
            "id": "remote",
            "base_url": "https://remote.example.test/v1",
            "models": {"szl-default": upstream},
            "token_env": "REMOTE_TOKEN",
        })


def test_public_alias_and_provider_id_remain_strict() -> None:
    for field, value in (("id", "org/remote"), ("models", {"org/szl-default": "org/model"})):
        record = {
            "id": "remote",
            "base_url": "https://remote.example.test/v1",
            "models": {"szl-default": "org/model"},
            "token_env": "REMOTE_TOKEN",
        }
        record[field] = value
        with pytest.raises(ValueError):
            module.ProviderRecord.model_validate(record)


def test_qualified_model_keeps_egress_and_classification_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = {
        "id": "remote",
        "base_url": "https://remote.example.test/v1",
        "models": {"szl-default": "Pro/zai-org/GLM-4.5"},
        "token_env": "REMOTE_TOKEN",
        "classifications": ["public"],
        "enabled": True,
    }
    monkeypatch.setenv("SZL_ROUTER_PROVIDERS_JSON", json.dumps({"providers": [provider]}))
    monkeypatch.setenv("SZL_ROUTER_ALLOWED_HOSTS", "remote.example.test")
    monkeypatch.setenv("SZL_ROUTER_TOKEN", "test-router-client")
    monkeypatch.setenv("REMOTE_TOKEN", "synthetic-secret")
    calls = 0

    async def forbidden(*args: Any, **kwargs: Any) -> tuple[dict[str, Any], int]:
        nonlocal calls
        calls += 1
        raise AssertionError("transport must remain unused")

    monkeypatch.setattr(module, "call_provider", forbidden)
    disabled = client.post("/v1/chat/completions", json=chat_request())
    assert disabled.status_code == 503
    assert disabled.json()["detail"]["code"] == "EGRESS_DISABLED"
    monkeypatch.setenv("SZL_ROUTER_ENABLE_EGRESS", "1")
    classified = client.post("/v1/chat/completions", json=chat_request(data_classification="confidential"))
    assert classified.status_code == 503
    assert classified.json()["detail"]["code"] == "NO_ELIGIBLE_PROVIDER"
    assert calls == 0


def test_plan_is_deterministic_and_sovereignty_first(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch)
    first = client.post(
        "/api/plan",
        json={"model": "szl-default", "data_classification": "public", "max_cost_tier": 10},
    )
    second = client.post(
        "/api/plan",
        json={"max_cost_tier": 10, "data_classification": "public", "model": "szl-default"},
    )
    assert first.status_code == 200 == second.status_code
    left = first.json()
    right = second.json()
    assert left["selected"] == "sovereign"
    assert [row["provider_id"] for row in left["candidates"]] == ["sovereign", "regional"]
    assert left["receipt"]["digest"] == right["receipt"]["digest"]


def test_plan_enforces_classification_and_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch)
    confidential = client.post(
        "/api/plan",
        json={"model": "szl-default", "data_classification": "confidential", "max_cost_tier": 10},
    ).json()
    assert [row["provider_id"] for row in confidential["candidates"]] == ["sovereign"]
    low_cost = client.post(
        "/api/plan",
        json={"model": "szl-default", "data_classification": "public", "max_cost_tier": 2},
    ).json()
    assert [row["provider_id"] for row in low_cost["candidates"]] == ["regional"]


def test_public_registry_never_returns_endpoint_or_token_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch, tokens=True)
    text = client.get("/api/routes").text
    assert "SOVEREIGN_TOKEN" not in text
    assert "REGIONAL_TOKEN" not in text
    assert "sovereign.example.test" not in text
    assert "regional.example.test" not in text
    payload = json.loads(text)
    assert payload["providers"][0]["credential_state"] == "AVAILABLE"


def test_chat_fails_closed_while_egress_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch, egress=False, tokens=True)
    response = client.post("/v1/chat/completions", json=chat_request())
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "EGRESS_DISABLED"


def test_successful_chat_attaches_exact_secret_free_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch, egress=True, tokens=True)

    async def fake_call(provider: module.ProviderRecord, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        assert provider.id == "sovereign"
        assert payload["model"] == "sovereign-model"
        return (
            {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "model": payload["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ready"}, "finish_reason": "stop"}],
            },
            200,
        )

    monkeypatch.setattr(module, "call_provider", fake_call)
    response = client.post("/v1/chat/completions", json=chat_request())
    assert response.status_code == 200
    payload = response.json()
    receipt = payload["szl_receipt"]
    assert receipt["provider_id"] == "sovereign"
    assert receipt["public_model"] == "szl-default"
    assert receipt["upstream_model"] == "sovereign-model"
    assert receipt["secret_material_recorded"] is False
    assert response.headers["x-szl-receipt"] == receipt["digest"]
    assert "sovereign-test-secret" not in response.text
    assert "regional-test-secret" not in response.text


def test_transport_failure_fails_over_in_deterministic_order(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch, egress=True, tokens=True)
    calls: list[str] = []

    async def fake_call(provider: module.ProviderRecord, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        calls.append(provider.id)
        if provider.id == "sovereign":
            raise RuntimeError("bounded transport failure")
        return (
            {
                "id": "chatcmpl-fallback",
                "object": "chat.completion",
                "model": payload["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "fallback"}, "finish_reason": "stop"}],
            },
            200,
        )

    monkeypatch.setattr(module, "call_provider", fake_call)
    response = client.post("/v1/chat/completions", json=chat_request())
    assert response.status_code == 200
    receipt = response.json()["szl_receipt"]
    assert calls == ["sovereign", "regional"]
    assert [row["state"] for row in receipt["attempts"]] == ["TRANSPORT_OR_CONTRACT_ERROR", "SUCCESS"]
    assert receipt["provider_id"] == "regional"


@pytest.mark.parametrize("status,expected_calls", [
    (302, ["sovereign", "regional"]),
    (429, ["sovereign", "regional"]),
    (400, ["sovereign"]),
])
def test_upstream_status_failover_respects_redirect_and_client_error_policy(
    monkeypatch: pytest.MonkeyPatch, status: int, expected_calls: list[str],
) -> None:
    configure(monkeypatch, egress=True, tokens=True)
    original_client = httpx.AsyncClient
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert request.url.host in {"sovereign.example.test", "regional.example.test"}
        provider = "sovereign" if request.url.host == "sovereign.example.test" else "regional"
        calls.append(provider)
        if provider == "sovereign":
            return httpx.Response(status, headers={"Location": "https://untrusted.example/"})
        return httpx.Response(200, json={
            "id": "chatcmpl-status-fallback", "object": "chat.completion",
            "model": "regional-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "fallback"},
                         "finish_reason": "stop"}],
        })

    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handle), **kwargs))
    response = client.post("/v1/chat/completions", json=chat_request())
    assert calls == expected_calls
    if status == 400:
        assert response.status_code == 502
        attempts = response.json()["detail"]["attempts"]
    else:
        assert response.status_code == 200
        attempts = response.json()["szl_receipt"]["attempts"]
        assert response.json()["szl_receipt"]["provider_id"] == "regional"
    assert attempts[0] == {"provider_id": "sovereign", "state": "UPSTREAM_HTTP_ERROR", "status_code": status}


def test_all_provider_failures_emit_bounded_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch, egress=True, tokens=True)

    async def fail(*_: Any, **__: Any) -> tuple[dict[str, Any], int]:
        raise RuntimeError("no transport")

    monkeypatch.setattr(module, "call_provider", fail)
    response = client.post("/v1/chat/completions", json=chat_request())
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["state"] == "ALL_ELIGIBLE_PROVIDERS_FAILED"
    assert detail["secret_material_recorded"] is False
    assert len(detail["digest"]) == 64


def test_streaming_and_extra_effect_fields_are_rejected() -> None:
    assert client.post("/v1/chat/completions", json=chat_request(stream=True)).status_code == 422
    assert client.post("/v1/chat/completions", json=chat_request(target_url="https://elsewhere.test")).status_code == 422


def test_frontend_is_local_only_and_adaptive() -> None:
    html = (module.STATIC / "index.html").read_text(encoding="utf-8")
    script = (module.STATIC / "app.js").read_text(encoding="utf-8")
    style = (module.STATIC / "styles.css").read_text(encoding="utf-8")
    assert 'href="#main"' in html
    assert "https://" not in html and "http://" not in html
    assert "localStorage" not in script and "sessionStorage" not in script
    assert "WebSocket(" not in script and "EventSource(" not in script
    assert "prefers-reduced-motion" in style
    assert "prefers-contrast" in style
    assert "forced-colors" in style
    assert "@media print" in style


def test_deployment_contract_does_not_claim_hub_publication() -> None:
    payload = client.get("/deployment.json").json()
    assert payload["runtime_state"] == "MEASURED_BY_THIS_RESPONSE"
    assert payload["hub_publication"].startswith("UNAVAILABLE")


@pytest.mark.parametrize("authorization", ["", "Bearer incorrect", "Basic test-router-client"])
def test_unauthorized_requests_never_reach_provider(monkeypatch, authorization):
    configure(monkeypatch, egress=True, tokens=True)

    async def forbidden(*args, **kwargs):
        pytest.fail("unauthorized caller reached provider")

    monkeypatch.setattr(module, "call_provider", forbidden)
    response = client.post("/v1/chat/completions", json=chat_request(), headers={"Authorization": authorization})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert "test-router-client" not in response.text


def test_egress_without_caller_auth_configuration_is_unavailable(monkeypatch):
    configure(monkeypatch, egress=True, tokens=True)
    monkeypatch.delenv("SZL_ROUTER_TOKEN")
    response = client.post("/v1/chat/completions", json=chat_request())
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "CALLER_AUTH_NOT_CONFIGURED"


def test_control_plane_readiness_is_separate_from_inference():
    control = client.get("/readyz")
    inference = client.get("/readyz/inference")
    assert control.status_code == 200
    assert control.json()["scope"] == "CONTROL_PLANE"
    assert control.json()["inference"]["ready_for_requests"] is False
    assert inference.status_code == 503
    assert inference.json()["inference_witness"] == "UNAVAILABLE"


def test_configured_inference_does_not_claim_provider_reachability(monkeypatch):
    configure(monkeypatch, egress=True, tokens=True)
    response = client.get("/readyz/inference")
    assert response.status_code == 200
    assert response.json()["basis"] == "LOCAL_CONFIGURATION_ONLY"
    assert response.json()["provider_reachability"] == "UNVERIFIED"
    monkeypatch.delenv("SOVEREIGN_TOKEN")
    monkeypatch.delenv("REGIONAL_TOKEN")
    assert client.get("/readyz/inference").status_code == 503


def test_disabled_providers_do_not_satisfy_readiness(monkeypatch):
    configure(monkeypatch, egress=True, tokens=True)
    registry = registry_payload()
    for provider in registry["providers"]:
        provider["enabled"] = False
    monkeypatch.setenv("SZL_ROUTER_PROVIDERS_JSON", json.dumps(registry))
    assert client.get("/readyz/inference").status_code == 503


def test_omitted_provider_enabled_stays_disabled_with_credential_and_egress(monkeypatch):
    provider = registry_payload()["providers"][0]
    provider.pop("enabled")
    monkeypatch.setenv("SZL_ROUTER_PROVIDERS_JSON", json.dumps({"providers": [provider]}))
    monkeypatch.setenv("SZL_ROUTER_ALLOWED_HOSTS", "regional.example.test")
    monkeypatch.setenv("SZL_ROUTER_ENABLE_EGRESS", "1")
    monkeypatch.setenv("SZL_ROUTER_TOKEN", "test-router-client")
    monkeypatch.setenv("REGIONAL_TOKEN", "synthetic-secret")
    calls = 0

    async def forbidden_transport(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("disabled provider reached transport")

    monkeypatch.setattr(module, "call_provider", forbidden_transport)
    routes = client.get("/api/routes").json()
    assert routes["state"] == "VALIDATED"
    assert routes["providers"][0]["enabled"] is False
    assert routes["providers"][0]["credential_state"] == "AVAILABLE"
    assert client.get("/readyz/inference").status_code == 503
    assert client.get("/v1/models").json()["data"] == []
    assert client.post("/api/plan", json={"model": "szl-default"}).json()["candidates"] == []
    response = client.post("/v1/chat/completions", json=chat_request())
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "NO_ELIGIBLE_PROVIDER"
    assert calls == 0


def test_invalid_configuration_does_not_echo_secret_input(monkeypatch):
    configure(monkeypatch, egress=True, tokens=True)
    registry = registry_payload()
    registry["accidental_private_value"] = "fixture-secret-never-echo"
    monkeypatch.setenv("SZL_ROUTER_PROVIDERS_JSON", json.dumps(registry))
    for path in ("/api/routes", "/readyz", "/deployment.json"):
        response = client.get(path)
        assert "fixture-secret-never-echo" not in response.text
        assert "accidental_private_value" not in response.text
    response = client.post("/v1/chat/completions", json=chat_request())
    assert response.status_code == 503
    assert "fixture-secret-never-echo" not in response.text


def test_hub_revision_cannot_impersonate_github_source(monkeypatch):
    monkeypatch.setenv("SPACE_COMMIT_SHA", "a" * 40)
    assert client.get("/api/source").json()["revision"] == "UNAVAILABLE"
    monkeypatch.setenv("SOURCE_REVISION", "b" * 40)
    response = client.get("/.well-known/szl-source.json")
    assert response.json()["revision"] == "b" * 40
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", ["/readyz", "/readyz/inference", "/deployment.json", "/api/routes"])
def test_operational_evidence_is_not_cacheable(path):
    assert client.get(path).headers["cache-control"] == "no-store"


@pytest.mark.parametrize("bad", [{"choices": []}, {"choices": None}, {"error": "private diagnostic"}, {"choices": [{}]}])
def test_malformed_upstream_answers_fail_over_before_receipting(monkeypatch, bad):
    configure(monkeypatch, egress=True, tokens=True)
    calls = []

    async def fake_call(provider, payload):
        calls.append(provider.id)
        if provider.id == "sovereign":
            return bad, 200
        return {"choices": [{"message": {"role": "assistant", "refusal": "Request declined."}}]}, 200

    monkeypatch.setattr(module, "call_provider", fake_call)
    response = client.post("/v1/chat/completions", json=chat_request())
    assert response.status_code == 200
    assert calls == ["sovereign", "regional"]
    payload = response.json()
    assert payload["choices"][0]["message"]["refusal"] == "Request declined."
    assert payload["szl_receipt"]["provider_id"] == "regional"
    assert payload["szl_receipt"]["attempts"][0]["state"] == "TRANSPORT_OR_CONTRACT_ERROR"
    assert "private diagnostic" not in response.text


@pytest.mark.parametrize("mutation", [
    {"base_url": "http://127.0.0.1:11434/v1"},
    {"base_url": "http://192.168.1.2:11434/v1"},
    {"token_env": "LOCAL_TOKEN"},
    {"model_digests": {}},
    {"model_digests": {"a11oy-mini-r2": "latest"}},
])
def test_loopback_registry_rejects_endpoint_credentials_or_unpinned_model(monkeypatch, mutation):
    configure_local(monkeypatch)
    registry = local_registry_payload()
    registry["providers"][0].update(mutation)
    monkeypatch.setenv("SZL_ROUTER_PROVIDERS_JSON", json.dumps(registry))
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["registry_state"] == "INVALID_FAIL_CLOSED"
    assert client.get("/api/routes").json()["providers"] == []


def test_local_configuration_and_provider_lock_preserve_policy(monkeypatch):
    configure_local(monkeypatch, with_remote=True)
    readiness = client.get("/readyz/inference")
    assert readiness.status_code == 200
    assert readiness.json()["ready_for_requests"] is True
    assert all(readiness.json()["checks"].values())
    assert readiness.json()["remote_credentialed_provider"] is True
    assert readiness.json()["provider_reachability"] == "UNVERIFIED"
    routes = client.get("/api/routes").json()
    local = next(row for row in routes["providers"] if row["id"] == "local-ollama")
    assert local["provider_type"] == "ollama_loopback"
    assert local["endpoint_state"] == "FIXED_LOOPBACK_UNVERIFIED"
    assert local["credential_state"] == "NOT_REQUIRED_LOOPBACK"
    assert local["model_identity_state"] == "PIN_CONFIGURED_UNVERIFIED"
    assert local["model_digests"] == {"a11oy-mini-r2": LOCAL_DIGEST}
    assert "127.0.0.1" not in json.dumps(routes)
    plan = client.post("/api/plan", json={
        "model": "a11oy-mini-r2", "required_provider_id": "local-ollama",
    }).json()
    assert [row["provider_id"] for row in plan["candidates"]] == ["local-ollama"]
    assert plan["required_provider_id"] == "local-ollama"
    assert plan["candidates"][0]["model_digest"] == LOCAL_DIGEST
    restricted = client.post("/api/plan", json={
        "model": "a11oy-mini-r2", "required_provider_id": "local-ollama",
        "data_classification": "restricted",
    }).json()
    assert restricted["candidates"] == []
    denied = client.post("/v1/chat/completions", json=chat_request(
        model="a11oy-mini-r2", required_provider_id="local-ollama",
        data_classification="restricted",
    ))
    assert denied.status_code == 503
    assert denied.json()["detail"]["code"] == "NO_ELIGIBLE_PROVIDER"


def test_loopback_configuration_readiness_does_not_claim_live_model(monkeypatch):
    configure_local(monkeypatch, egress=False)

    async def forbidden_inventory(*args, **kwargs):
        pytest.fail("disabled router probed Ollama")

    monkeypatch.setattr(module, "ollama_inventory", forbidden_inventory)
    assert client.get("/readyz/inference").status_code == 503
    local = client.get("/api/local-models").json()
    assert local["status"] == "unavailable"
    assert local["providers"][0]["inventory_state"] == "EGRESS_DISABLED"
    assert local["providers"][0]["models"][0]["state"] == "UNAVAILABLE"
    monkeypatch.setenv("SZL_ROUTER_ENABLE_EGRESS", "1")
    admission = client.get("/readyz/inference")
    assert admission.status_code == 200
    assert all(admission.json()["checks"].values())
    assert admission.json()["remote_credentialed_provider"] is False
    assert admission.json()["inference_witness"] == "UNAVAILABLE"


def test_fixed_loopback_inventory_chat_residency_and_receipt(monkeypatch):
    configure_local(monkeypatch)
    original_client = httpx.AsyncClient
    calls: list[str] = []
    generated = False

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal generated
        assert request.url.scheme == "http"
        assert request.url.host == "127.0.0.1"
        assert request.url.port == 11434
        assert "authorization" not in request.headers
        calls.append(request.url.path)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [
                {"name": LOCAL_UPSTREAM, "digest": LOCAL_DIGEST},
                {"name": "hf.co/SZLHOLDINGS/unrelated:latest", "digest": OTHER_DIGEST},
            ]})
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [
                {"name": LOCAL_UPSTREAM, "digest": LOCAL_DIGEST},
            ] if generated else []})
        assert request.url.path == "/v1/chat/completions"
        sent = json.loads(request.content)
        assert sent["model"] == LOCAL_UPSTREAM
        generated = True
        return httpx.Response(200, json={
            "id": "chatcmpl-local-test", "object": "chat.completion", "model": LOCAL_UPSTREAM,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "local answer"},
                         "finish_reason": "stop"}],
        })

    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handle), **kwargs))
    inventory = client.get("/api/local-models")
    assert inventory.status_code == 200
    model = inventory.json()["providers"][0]["models"][0]
    assert inventory.json()["basis"] == "LOCAL_DAEMON_REPORTED"
    assert model["state"] == "MATCH"
    assert model["resident_state"] == "NOT_LOADED"
    original_request = chat_request(model="a11oy-mini-r2", required_provider_id="local-ollama")
    response = client.post("/v1/chat/completions", json=original_request)
    assert response.status_code == 200
    completion = response.json()
    receipt = completion["szl_receipt"]
    identity = receipt["model_identity"]
    assert identity["expected_digest"] == LOCAL_DIGEST
    assert identity["pre_request_manifest_digest"] == LOCAL_DIGEST
    assert identity["pre_request_resident_digest"] is None
    assert identity["post_request_manifest_digest"] == LOCAL_DIGEST
    assert identity["post_request_resident_digest"] == LOCAL_DIGEST
    assert identity["independent_attestation"] == "UNAVAILABLE"
    assert identity["license_state"] == "NOT_VERIFIED"
    assert calls == ["/api/tags", "/api/ps", "/api/tags", "/api/ps",
                     "/v1/chat/completions", "/api/tags", "/api/ps"]
    verification = client.post("/api/verify", json={
        "completion": completion, "request": original_request,
    }, headers={"X-SZL-Receipt": response.headers["x-szl-receipt"]})
    assert verification.status_code == 200
    assert verification.json()["status"] == "CONSISTENT"


@pytest.mark.parametrize("change_after_chat,expected_state", [
    (False, "MODEL_DIGEST_MISMATCH"),
    (True, "MODEL_DIGEST_MISMATCH"),
])
def test_local_digest_mismatch_stops_before_cloud_fallback(monkeypatch, change_after_chat,
                                                            expected_state):
    configure_local(monkeypatch, with_remote=True)
    original_client = httpx.AsyncClient
    calls: list[str] = []
    generated = False

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal generated
        calls.append(request.url.path)
        assert request.url.host == "127.0.0.1"
        if request.url.path == "/api/tags":
            digest = OTHER_DIGEST if generated or not change_after_chat else LOCAL_DIGEST
            return httpx.Response(200, json={"models": [{"name": LOCAL_UPSTREAM, "digest": digest}]})
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [
                {"name": LOCAL_UPSTREAM, "digest": LOCAL_DIGEST},
            ] if generated else []})
        assert request.url.path == "/v1/chat/completions"
        generated = True
        return httpx.Response(200, json={
            "model": LOCAL_UPSTREAM,
            "choices": [{"message": {"role": "assistant", "content": "answer"}}],
        })

    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handle), **kwargs))
    response = client.post("/v1/chat/completions", json=chat_request(model="a11oy-mini-r2"))
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["state"] == "LOCAL_MODEL_IDENTITY_UNAVAILABLE"
    assert detail["attempts"] == [{"provider_id": "local-ollama", "state": expected_state}]
    assert "/v1/chat/completions" in calls if change_after_chat else "/v1/chat/completions" not in calls
    assert "remote-test-secret" not in response.text


def test_metadata_phase_is_inside_attempt_and_global_route_deadlines(monkeypatch):
    configure_local(monkeypatch)

    async def slow_inventory(*args, **kwargs):
        await asyncio.sleep(0.05)
        return {}

    monkeypatch.setattr(module, "ollama_inventory", slow_inventory)
    monkeypatch.setattr(module, "MAX_TIMEOUT_SECONDS", 0.01)
    response = client.post("/v1/chat/completions", json=chat_request(model="a11oy-mini-r2"))
    assert response.status_code == 502
    assert response.json()["detail"]["attempts"][0]["state"] == "UPSTREAM_DEADLINE_EXCEEDED"
    monkeypatch.setattr(module, "MAX_ROUTE_SECONDS", 0.002)
    response = client.post("/v1/chat/completions", json=chat_request(model="a11oy-mini-r2"))
    assert response.status_code == 502
    assert response.json()["detail"]["attempts"][0]["state"] == "ROUTE_DEADLINE_EXCEEDED"


@pytest.mark.parametrize("body", [
    b'{"models":[],"junk":' + b'9' * 5000 + b'}',
    b'{"models":[],"junk":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}',
    b'{"models":[],"models":[]}',
])
def test_bounded_malformed_daemon_inventory_is_sanitized(monkeypatch, body):
    configure_local(monkeypatch)
    original_client = httpx.AsyncClient

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "127.0.0.1"
        assert request.url.path == "/api/tags"
        return httpx.Response(200, content=body)

    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handle), **kwargs))
    inventory = client.get("/api/local-models")
    assert inventory.status_code == 200
    assert inventory.json()["providers"][0]["inventory_state"] == "UNAVAILABLE"
    response = client.post("/v1/chat/completions", json=chat_request(model="a11oy-mini-r2"))
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["state"] == "LOCAL_MODEL_IDENTITY_UNAVAILABLE"
    assert detail["attempts"] == [{"provider_id": "local-ollama", "state": "DAEMON_INVENTORY_INVALID"}]
    assert len(detail["digest"]) == 64
    assert "999999" not in response.text


def test_deep_upstream_json_emits_failure_receipt_instead_of_server_error(monkeypatch):
    configure(monkeypatch, egress=True, tokens=True)
    original_client = httpx.AsyncClient
    body = b'{"choices":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}'

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "sovereign.example.test"
        return httpx.Response(200, content=body)

    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handle), **kwargs))
    response = client.post("/v1/chat/completions", json=chat_request(
        required_provider_id="sovereign"))
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["state"] == "ALL_ELIGIBLE_PROVIDERS_FAILED"
    assert detail["attempts"] == [{
        "provider_id": "sovereign", "state": "TRANSPORT_OR_CONTRACT_ERROR",
        "error_type": "RuntimeError",
    }]
    assert len(detail["digest"]) == 64


def test_local_model_probe_is_bounded_shared_and_config_keyed(monkeypatch):
    configure_local(monkeypatch)
    calls: list[str] = []

    async def delayed_inventory(kind):
        calls.append(kind)
        await asyncio.sleep(0.03)
        if kind == "tags":
            return {LOCAL_UPSTREAM: LOCAL_DIGEST}
        return {}

    monkeypatch.setattr(module, "ollama_inventory", delayed_inventory)

    async def exercise():
        first_task = asyncio.create_task(module.local_models())
        await asyncio.sleep(0.005)
        busy = await module.local_models()
        first = await first_task
        cached = await module.local_models()
        registry = local_registry_payload()
        registry["providers"][0]["model_digests"]["a11oy-mini-r2"] = OTHER_DIGEST
        monkeypatch.setenv("SZL_ROUTER_PROVIDERS_JSON", json.dumps(registry))
        changed = await module.local_models()
        return first, busy, cached, changed

    first, busy, cached, changed = asyncio.run(exercise())
    assert first["observation_state"] == "LIVE_PROBE"
    assert first["providers"][0]["models"][0]["state"] == "MATCH"
    assert busy["observation_state"] == "BUSY"
    assert busy["providers"][0]["inventory_state"] == "BUSY"
    assert busy["providers"][0]["models"][0]["state"] == "UNAVAILABLE"
    assert cached["observation_state"] == "CACHED_RECENT"
    assert cached["observed_at"] == first["observed_at"]
    assert cached["observation_age_ms"] is not None
    assert changed["observation_state"] == "LIVE_PROBE"
    assert changed["providers"][0]["models"][0]["state"] == "DIGEST_MISMATCH"
    assert calls == ["tags", "ps", "tags", "ps"]
