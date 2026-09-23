"""Offline cache isolation regressions; the fake transport observes actual calls."""
from dataclasses import replace
import json
from unittest.mock import Mock

import pytest

from szl_router import core


@pytest.fixture
def routing(monkeypatch):
    monkeypatch.setenv("EMBED_TEST_URL", "http://first.invalid/v1")
    monkeypatch.setenv("EMBED_TEST_KEY", "fake-upstream-key-a")
    monkeypatch.setenv("SZL_ROUTER_TOKEN", "fake-router-key-a")
    monkeypatch.setenv("SZL_EMBED_CACHE_NAMESPACE", "test-generation-a")
    primary = core.Provider("primary", "EMBED_TEST_URL", "", "EMBED_TEST_KEY",
                            True, "self-hosted")
    fallback = core.Provider("fallback", "", "http://fallback.invalid/v1", "",
                             False, "grid")
    monkeypatch.setattr(core, "PROVIDERS", {"primary": primary, "fallback": fallback})
    monkeypatch.setattr(core, "EMBED_ROUTES", {
        "bge-large": [("primary", "weights-a"), ("fallback", "weights-b")],
    })
    monkeypatch.setattr(core, "_EMBED_CACHE_MAX", 1024)
    monkeypatch.setattr(core, "_EMBED_CACHE_TTL", 300)
    calls = []

    def post(provider, payload, timeout):
        calls.append((provider.name, provider.base_url(), payload.copy()))
        input_ = payload["input"]
        count = (len(input_) if isinstance(input_, list) and input_
                 and isinstance(input_[0], (str, list)) else 1)
        return {"model": payload["model"], "data": [
            {"index": i, "embedding": [float(len(calls)), float(i)]}
            for i in range(count)
        ]}

    monkeypatch.setattr(core, "_post_embeddings", post)
    core.embed_cache_clear()
    yield calls, post
    core.embed_cache_clear()


def test_same_route_and_reordered_extra_hit(routing):
    calls, _ = routing
    first = core.embed("bge-large", "document", extra={"user": "a", "encoding_format": "float"})
    second = core.embed("bge-large", "document", extra={"encoding_format": "float", "user": "a"})
    assert len(calls) == 1
    assert first["data"] == second["data"]
    assert second["x_szl_cache"]["hit"] is True
    assert second["model"] == "weights-a"
    assert second["x_szl_provenance"]["base_url"] == "http://first.invalid/v1"


@pytest.mark.parametrize("change", ["endpoint", "upstream_model", "provider", "labels",
                                    "upstream_key", "caller_key", "namespace"])
def test_effective_configuration_change_misses(routing, monkeypatch, change):
    calls, _ = routing
    first = core.embed("bge-large", "document")
    if change == "endpoint":
        monkeypatch.setenv("EMBED_TEST_URL", "http://second.invalid/v1")
    elif change == "upstream_model":
        core.EMBED_ROUTES["bge-large"][0] = ("primary", "weights-new")
    elif change == "provider":
        core.EMBED_ROUTES["bge-large"].reverse()
    elif change == "labels":
        core.PROVIDERS["primary"] = replace(core.PROVIDERS["primary"], sovereign=False, energy_source="grid")
    elif change == "upstream_key":
        monkeypatch.setenv("EMBED_TEST_KEY", "fake-upstream-key-b")
    elif change == "caller_key":
        monkeypatch.setenv("SZL_ROUTER_TOKEN", "fake-router-key-b")
    else:
        monkeypatch.setenv("SZL_EMBED_CACHE_NAMESPACE", "test-generation-b")
    second = core.embed("bge-large", "document")
    assert len(calls) == 2
    assert "x_szl_cache" not in second
    assert second["data"] != first["data"]
    assert core.embed("bge-large", "document")["x_szl_cache"]["hit"] is True
    assert len(calls) == 2


def test_unavailable_route_cannot_replay_its_cache(routing, monkeypatch):
    calls, _ = routing
    core.embed("bge-large", "document")
    monkeypatch.delenv("EMBED_TEST_KEY")
    second = core.embed("bge-large", "document")
    assert len(calls) == 2
    assert second["x_szl_provenance"]["provider"] == "fallback"
    assert second["x_szl_provenance"]["sovereign"] is False


def test_registry_alias_change_cannot_relabel_cached_origin(routing):
    calls, _ = routing
    core.embed("bge-large", "document")
    core.PROVIDERS["alias"] = core.PROVIDERS["primary"]
    core.EMBED_ROUTES["bge-large"] = [("alias", "weights-a")]
    result = core.embed("bge-large", "document")
    assert len(calls) == 2
    assert result["x_szl_provenance"]["provider"] == "alias"


def test_cached_fallback_does_not_bypass_recovered_primary(routing, monkeypatch):
    calls, _ = routing
    monkeypatch.delenv("EMBED_TEST_KEY")
    core.embed("bge-large", "document")
    monkeypatch.setenv("EMBED_TEST_KEY", "fake-upstream-key-a")
    result = core.embed("bge-large", "document")
    assert [call[0] for call in calls] == ["fallback", "primary"]
    assert result["model"] == "weights-a"


def test_transport_failure_recovery_is_not_hidden_by_cached_fallback(routing, monkeypatch):
    calls, post = routing

    def down(provider, payload, timeout):
        if provider.name == "primary":
            raise OSError("offline test failure")
        return post(provider, payload, timeout)

    monkeypatch.setattr(core, "_post_embeddings", down)
    first = core.embed("bge-large", "document")
    assert first["x_szl_provenance"]["provider"] == "fallback"
    monkeypatch.setattr(core, "_post_embeddings", post)
    second = core.embed("bge-large", "document")
    assert second["x_szl_provenance"]["provider"] == "primary"
    assert len(calls) == 2


def test_entire_route_unavailable_fails_despite_warm_cache(routing, monkeypatch):
    core.EMBED_ROUTES["bge-large"] = [("primary", "weights-a")]
    core.embed("bge-large", "document")
    monkeypatch.delenv("EMBED_TEST_KEY")
    with pytest.raises(core.RouterError):
        core.embed("bge-large", "document")


def test_endpoint_and_credential_are_frozen_for_transport_and_provenance(routing, monkeypatch):
    calls, post = routing
    seen = []

    def change_environment(provider, payload, timeout):
        monkeypatch.setenv("EMBED_TEST_URL", "http://changed.invalid/v1")
        monkeypatch.setenv("EMBED_TEST_KEY", "fake-upstream-key-b")
        seen.append((provider.base_url(), provider.api_key()))
        return post(provider, payload, timeout)

    monkeypatch.setattr(core, "_post_embeddings", change_environment)
    first = core.embed("bge-large", "document")
    assert seen[0] == ("http://first.invalid/v1", "fake-upstream-key-a")
    assert first["x_szl_provenance"]["base_url"] == "http://first.invalid/v1"
    core.embed("bge-large", "document")
    assert len(calls) == 2


class LooksLikeText:
    def __str__(self):
        return "document"


@pytest.mark.parametrize("bad", [LooksLikeText(), ("document",), {1: "a"},
                                 float("nan"), float("inf"), {"nested": [float("nan")]}])
@pytest.mark.parametrize("use_cache", [True, False])
def test_ambiguous_or_non_json_input_rejected_before_transport(routing, bad, use_cache):
    calls, _ = routing
    core.embed("bge-large", "document")
    with pytest.raises(ValueError):
        core.embed("bge-large", bad, use_cache=use_cache)
    assert len(calls) == 1


@pytest.mark.parametrize("extra", [{"model": "other"}, {"input": "other"},
                                   {1: "value"}, {"nested": {1: "value"}}, []])
def test_extra_overrides_and_non_json_keys_rejected(routing, extra):
    calls, _ = routing
    with pytest.raises(ValueError):
        core.embed("bge-large", "document", extra=extra)
    assert calls == []


def test_input_types_and_batch_order_remain_distinct(routing):
    calls, _ = routing
    # All are JSON-native but represent different requests. A token list and a
    # text list must not share identity even when their printed values resemble.
    for input_ in ("1", [1], ["1"], [[1]], ["a", "b"], ["b", "a"]):
        core.embed("bge-large", input_)
    assert len(calls) == 6


def test_json_scalar_types_in_extra_remain_distinct(routing):
    calls, _ = routing
    for value in (True, 1, 1.0, "1", None):
        core.embed("bge-large", "document", extra={"parameter": value})
    assert len(calls) == 5


def test_initial_response_mutation_does_not_poison_cache(routing):
    first = core.embed("bge-large", "document")
    first["data"][0]["embedding"][0] = 999.0
    first["x_szl_provenance"]["sovereign"] = False
    second = core.embed("bge-large", "document")
    assert second["data"][0]["embedding"][0] == 1.0
    assert second["x_szl_provenance"]["sovereign"] is True


def test_observed_response_model_is_preserved_without_revision_claim(routing, monkeypatch):
    _, post = routing

    def revision(provider, payload, timeout):
        result = post(provider, payload, timeout)
        result["model"] = "provider-reported-revision-42"
        return result

    monkeypatch.setattr(core, "_post_embeddings", revision)
    first = core.embed("bge-large", "document")
    cached = core.embed("bge-large", "document")
    assert first["model"] == cached["model"] == "provider-reported-revision-42"
    assert cached["x_szl_provenance"]["upstream_model"] == "weights-a"
    assert "weights_revision_verified" not in cached


def test_cache_keys_and_response_do_not_contain_credentials(routing):
    result = core.embed("bge-large", "document")
    visible = json.dumps([list(core._EMBED_CACHE), result])
    assert "fake-upstream-key-a" not in visible
    assert "fake-router-key-a" not in visible
    assert all(len(key) == 64 for key in core._EMBED_CACHE)


def test_http_maps_invalid_request_to_400_without_upstream(monkeypatch):
    from fastapi.testclient import TestClient
    from szl_router import app

    monkeypatch.delenv("SZL_ROUTER_TOKEN", raising=False)
    transport = Mock(side_effect=AssertionError("must not contact upstream"))
    monkeypatch.setattr(core, "_post_embeddings", transport)
    response = TestClient(app.app).post("/v1/embeddings", json={"model": ["bad"], "input": "document"})
    assert response.status_code == 400
    assert response.json()["detail"] == "embeddings model must be a non-empty string"
    transport.assert_not_called()
