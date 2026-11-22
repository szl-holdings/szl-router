# SPDX-License-Identifier: Apache-2.0
"""CPU-only protocol-contract tests; every network exchange uses MockTransport."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from router_control import app as router
from router_control.provider_protocols import prepare_provider_request, normalize_provider_response

CLIENT = TestClient(router.app)
KEY = "synthetic-public-test-only"
COMMON = {"model": "public-bridge", "messages": [{"role": "user", "content": "Describe a public comet."}],
          "max_tokens": 96, "stream": False}


def configure(monkeypatch, provider_type, hostname, upstream_model):
    monkeypatch.setenv("SZL_ROUTER_ENABLE_EGRESS", "1")
    monkeypatch.setenv("SZL_ROUTER_TOKEN", KEY)
    monkeypatch.setenv("SZL_ROUTER_ALLOWED_HOSTS", hostname)
    monkeypatch.setenv("SZL_TEST_UPSTREAM_KEY", "synthetic-upstream-key")
    monkeypatch.setenv("SZL_ROUTER_PROVIDERS_JSON", json.dumps({"providers": [{
        "id": "bridge", "provider_type": provider_type,
        "base_url": "https://" + hostname, "models": {"public-bridge": upstream_model},
        "token_env": "SZL_TEST_UPSTREAM_KEY", "classifications": ["public"],
        "cost_tier": 9, "sovereignty": 0, "enabled": True,
    }]}))


def anthropic_reply():
    return {"id": "msg_test", "type": "message", "role": "assistant",
            "model": "claude-example", "content": [{"type": "text", "text": "Public comet."}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 8, "output_tokens": 4}}


def agent_reply():
    return {"id": "resp_test", "object": "response", "status": "completed", "error": None,
            "incomplete_details": None, "model": "openai/example",
            "output": [{"type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": "Public comet.", "annotations": []}]}],
            "usage": {"input_tokens": 9, "output_tokens": 4}}


@pytest.mark.parametrize("provider_type,hostname,upstream_model,route,reply", [
    ("anthropic_messages_https", "api.anthropic.com", "claude-example", "/v1/messages", anthropic_reply),
    ("perplexity_agent_https", "api.perplexity.ai", "openai/example", "/v1/agent", agent_reply),
])
def test_bound_text_protocols_have_true_transport_and_verified_receipts(
    monkeypatch, provider_type, hostname, upstream_model, route, reply,
):
    configure(monkeypatch, provider_type, hostname, upstream_model)
    client_ctor = httpx.AsyncClient
    sent = []
    def responder(request):
        sent.append(request)
        assert request.url.host == hostname and request.url.path == route
        assert request.headers["authorization"] == "Bearer synthetic-upstream-key"
        assert request.headers.get("anthropic-version") == (
            "2026-10-21" if provider_type == "anthropic_messages_https" else None)
        body = json.loads(request.content)
        assert body["model"] == upstream_model
        assert "user" not in body and "target_url" not in body
        assert body["max_tokens"] == 96 if provider_type == "anthropic_messages_https" else body["max_output_tokens"] == 96
        assert body.get("tools", []) == []
        return httpx.Response(200, json=reply())
    monkeypatch.setattr(router.httpx, "AsyncClient", lambda **kwargs: client_ctor(
        transport=httpx.MockTransport(responder), **kwargs))
    request = {**COMMON, "required_provider_id": "bridge", "max_cost_tier": 10}
    response = CLIENT.post("/v1/chat/completions", json=request, headers={"Authorization": "Bearer " + KEY})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["choices"][0]["message"]["content"] == "Public comet."
    assert data["szl_receipt"]["provider_id"] == "bridge"
    assert data["szl_receipt"]["secret_material_recorded"] is False
    assert len(data["szl_receipt"]["digest"]) == 64
    assert "synthetic-upstream-key" not in response.text
    assert len(sent) == 1


@pytest.mark.parametrize("kind,bad", [
    ("anthropic_messages_https", {"max_tokens": None}),
    ("perplexity_agent_https", {"max_tokens": 4097}),
    ("perplexity_agent_https", {"stop": "stop"}),
    ("perplexity_agent_https", {"messages": [{"role": "assistant", "content": "not a user"}]}),
    ("anthropic_messages_https", {"messages": [{"role": "tool", "content": "forbidden"}]}),
    ("anthropic_messages_https", {"user": "private-identifier"}),
])
def test_unsupported_request_semantics_fail_before_transport(kind, bad):
    with pytest.raises(ValueError):
        prepare_provider_request(kind, {**COMMON, **bad})


def test_non_text_and_unfinished_responses_never_become_answers():
    broken = anthropic_reply()
    broken["content"] = [{"type": "tool_use", "name": "sensitive_action"}]
    with pytest.raises(ValueError):
        normalize_provider_response("anthropic_messages_https", broken)
    broken = anthropic_reply()
    broken["content"][0]["citations"] = [{"type": "web_search_result_location", "url": "https://example.test"}]
    with pytest.raises(ValueError):
        normalize_provider_response("anthropic_messages_https", broken)
    broken = agent_reply()
    broken["output"].append({"type": "search_results", "results": [{"url": "private"}]})
    with pytest.raises(ValueError):
        normalize_provider_response("perplexity_agent_https", broken)
    broken = agent_reply()
    broken["output"][0]["content"][0]["annotations"] = [{"type": "url_citation", "url": "https://example.test"}]
    with pytest.raises(ValueError):
        normalize_provider_response("perplexity_agent_https", broken)
    broken = agent_reply()
    broken["status"] = "incomplete"
    with pytest.raises(ValueError):
        normalize_provider_response("perplexity_agent_https", broken)


@pytest.mark.parametrize("kind,hostname", [
    ("anthropic_messages_https", "proxy.example.org"),
    ("perplexity_agent_https", "proxy.example.org"),
])
def test_vendor_specific_protocols_reject_other_even_allowlisted_hosts(monkeypatch, kind, hostname):
    configure(monkeypatch, kind, hostname, "upstream")
    assert CLIENT.get("/api/routes").json()["state"] == "INVALID_FAIL_CLOSED"
    assert CLIENT.get("/readyz/inference").status_code == 503


def test_protocol_bridges_default_to_no_route_without_live_configuration(monkeypatch):
    monkeypatch.delenv("SZL_ROUTER_PROVIDERS_JSON", raising=False)
    monkeypatch.delenv("SZL_ROUTER_ENABLE_EGRESS", raising=False)
    assert CLIENT.post("/v1/chat/completions", json=COMMON).status_code == 503
