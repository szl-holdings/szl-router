# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from router_control import app as module

client = TestClient(module.app)
CONFIG_VARS = (
    "SZL_ROUTER_ALLOWED_HOSTS",
    "SZL_ROUTER_PROVIDERS_JSON",
    "SZL_ROUTER_ENABLE_EGRESS",
    "SOVEREIGN_TOKEN",
    "REGIONAL_TOKEN",
)


@pytest.fixture(autouse=True)
def clean_router_environment(monkeypatch: pytest.MonkeyPatch) -> None:
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
