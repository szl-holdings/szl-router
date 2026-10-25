"""Captured embedding providers must keep the operator opt-in. No provider I/O."""

import pytest

from szl_router import core


def _install(monkeypatch, primary_enable, fallback_enable=""):
    monkeypatch.setenv("SZL_ROUTER_TOKEN", "fake-router-key")
    monkeypatch.setenv("SZL_EMBED_CACHE_NAMESPACE", "enable-env-test")
    monkeypatch.delenv("SZL_LOCAL_OLLAMA_ENABLE", raising=False)
    monkeypatch.delenv("SZL_OPT_IN", raising=False)
    primary = core.Provider(
        "primary", "", "http://primary.invalid/v1", "",
        True, "self-hosted", enable_env=primary_enable,
    )
    fallback = core.Provider(
        "fallback", "", "http://fallback.invalid/v1", "",
        True, "self-hosted", enable_env=fallback_enable,
    )
    monkeypatch.setattr(core, "PROVIDERS", {"primary": primary, "fallback": fallback})
    monkeypatch.setattr(core, "EMBED_ROUTES", {
        "bge-large": [("primary", "weights-a"), ("fallback", "weights-b")],
    })
    calls = []

    def post(provider, payload, timeout):
        calls.append(provider.name)
        return {"model": payload["model"], "data": [{"index": 0, "embedding": [1.0]}]}

    monkeypatch.setattr(core, "_post_embeddings", post)
    core.embed_cache_clear()
    return calls


def test_capture_retains_enable_env():
    provider = core.Provider(
        "local_ollama", "", "http://127.0.0.1:11434/v1", "",
        True, "self-hosted", enable_env="SZL_LOCAL_OLLAMA_ENABLE",
    )
    captured = core._ResolvedEmbeddingProvider.capture(provider)
    assert captured.enable_env == "SZL_LOCAL_OLLAMA_ENABLE"
    assert captured.base_url() == "http://127.0.0.1:11434/v1"


def test_disabled_provider_is_not_dispatched(monkeypatch):
    calls = _install(monkeypatch, "SZL_LOCAL_OLLAMA_ENABLE")
    result = core.embed("bge-large", "document")
    assert calls == ["fallback"]
    assert result["x_szl_provenance"]["provider"] == "fallback"


def test_disabled_provider_warm_cache_is_not_served(monkeypatch):
    calls = _install(
        monkeypatch, "SZL_LOCAL_OLLAMA_ENABLE", "SZL_LOCAL_OLLAMA_ENABLE",
    )
    monkeypatch.setenv("SZL_LOCAL_OLLAMA_ENABLE", "1")
    first = core.embed("bge-large", "document")
    assert calls == ["primary"]
    assert "x_szl_cache" not in first
    monkeypatch.delenv("SZL_LOCAL_OLLAMA_ENABLE")
    with pytest.raises(core.RouterError):
        core.embed("bge-large", "document")
    assert calls == ["primary"]


def test_enabled_provider_keeps_expected_dispatch(monkeypatch):
    calls = _install(monkeypatch, "SZL_LOCAL_OLLAMA_ENABLE")
    monkeypatch.setenv("SZL_LOCAL_OLLAMA_ENABLE", "true")
    result = core.embed("bge-large", "document")
    assert calls == ["primary"]
    assert result["x_szl_provenance"]["provider"] == "primary"
    assert result["x_szl_provenance"]["base_url"] == "http://primary.invalid/v1"


def test_fallback_cannot_bypass_opt_in(monkeypatch):
    calls = _install(monkeypatch, "SZL_OPT_IN", "SZL_OPT_IN")
    with pytest.raises(core.RouterError):
        core.embed("bge-large", "document")
    assert calls == []
