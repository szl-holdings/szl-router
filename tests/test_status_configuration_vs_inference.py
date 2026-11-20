"""Readiness honesty: configured provider must never imply successful inference."""
from szl_router import core


def test_configured_but_unreachable_provider_never_claims_inference(monkeypatch):
    # 127.0.0.1:1 is deliberately not contacted. No paid or remote request.
    monkeypatch.setenv("A11OY_MODEL_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("A11OY_GPU_TOKEN", "test-only-not-an-auth-secret")
    providers = {p["provider"]: p for p in core.status()["providers"]}
    box = providers["box_gpu"]
    assert box["available"] is True
    assert box["availability_basis"] == "configuration_only"
    assert box["inference_readiness"] == "NOT_PROBED"


def test_unarmed_provider_is_not_a_negative_inference_probe(monkeypatch):
    monkeypatch.delenv("SZL_LOCAL_OLLAMA_ENABLE", raising=False)
    providers = {p["provider"]: p for p in core.status()["providers"]}
    local = providers["local_ollama"]
    assert local["available"] is False
    assert local["availability_basis"] == "configuration_only"
    assert local["inference_readiness"] == "NOT_PROBED"


def test_readiness_metadata_is_not_specific_to_one_provider():
    for row in core.status()["providers"]:
        assert row["availability_basis"] == "configuration_only"
        assert row["inference_readiness"] == "NOT_PROBED"
