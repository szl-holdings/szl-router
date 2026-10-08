# SPDX-License-Identifier: Apache-2.0
"""Tiny-file contract tests for opt-in Ollama store byte admission."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from router_control import app as router
from router_control import local_store


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def store_fixture(tmp_path: Path, model: str = "szl-khipu:latest") -> tuple[Path, str, str]:
    root = tmp_path / "ollama-models"
    blobs = root / "blobs"
    blobs.mkdir(parents=True)
    layers = []
    for kind, data in (("model", b"tiny GGUF fixture"),
                       ("license", b"Apache-2.0 fixture")):
        digest = sha(data)
        (blobs / ("sha256-" + digest)).write_bytes(data)
        layers.append({"mediaType": "application/vnd.ollama.image." + kind,
                       "digest": "sha256:" + digest, "size": len(data)})
    config = b'{"fixture":true}'
    (blobs / ("sha256-" + sha(config))).write_bytes(config)
    manifest = {"schemaVersion": 2,
                "config": {"mediaType": "application/vnd.docker.container.image.v1+json",
                           "digest": "sha256:" + sha(config), "size": len(config)},
                "layers": layers}
    path = root / local_store.manifest_relative_path(model)
    path.parent.mkdir(parents=True)
    raw = json.dumps(manifest, separators=(",", ":")).encode()
    path.write_bytes(raw)
    return root, sha(raw), layers[0]["digest"][7:]


def observe(root: Path, manifest: str, weight: str):
    return local_store.verify_store_bytes("szl-khipu:latest", manifest, weight, str(root))


def test_read_only_byte_admission_hashes_all_layers(tmp_path):
    root, manifest, weight = store_fixture(tmp_path)
    result = observe(root, manifest, weight)
    assert result["state"] == "LOCAL_STORE_BYTES_PRE_REQUEST_MATCH"
    assert result["manifest_sha256"] == manifest
    assert result["weight_sha256"] == weight
    assert result["verified_blob_count"] == 3
    assert result["observation_scope"] == "SEQUENTIAL_PRE_REQUEST_READS"
    assert result["loaded_weight_attestation"] == "UNAVAILABLE"
    assert result["post_completion_store_check"] == "NOT_PERFORMED"
    assert str(root) not in json.dumps(result)


@pytest.mark.parametrize("model", [
    "../elsewhere:latest", "library/../elsewhere:latest", "a//b:latest",
    "hf.co/owner/model/../../other:latest", "C:\\model:latest",
    "hf.co/owner/model:bad:tag", "hf.co/owner/model/",
])
def test_byte_admission_rejects_pathlike_aliases(model):
    with pytest.raises(ValueError):
        local_store.manifest_relative_path(model)


def test_hub_alias_resolves_inside_store():
    assert local_store.manifest_relative_path(
        "hf.co/SZLHOLDINGS/SZL-Khipu-1.5B-GGUF:Q4_K_M"
    ).parts == ("manifests", "hf.co", "SZLHOLDINGS", "SZL-Khipu-1.5B-GGUF", "Q4_K_M")


@pytest.mark.parametrize("change,state", [
    ("weight", "LOCAL_STORE_BLOB_MISMATCH"),
    ("license", "LOCAL_STORE_BLOB_MISMATCH"),
    ("missing", "LOCAL_STORE_FILE_UNAVAILABLE"),
    ("directory", "LOCAL_STORE_PATH_INVALID"),
    ("manifest", "LOCAL_STORE_MANIFEST_MISMATCH"),
    ("pin", "LOCAL_STORE_WEIGHT_PIN_MISMATCH"),
])
def test_rejects_changed_or_missing_bytes(tmp_path, change, state):
    root, manifest, weight = store_fixture(tmp_path)
    path = root / local_store.manifest_relative_path("szl-khipu:latest")
    data = json.loads(path.read_text())
    model_blob = root / "blobs" / ("sha256-" + weight)
    if change == "weight":
        model_blob.write_bytes(b"tiny GGUF fixturE")
    elif change == "license":
        license_blob = root / "blobs" / ("sha256-" + data["layers"][1]["digest"][7:])
        license_blob.write_bytes(b"Apache-2.0 fixturE")
    elif change == "missing":
        model_blob.unlink()
    elif change == "directory":
        model_blob.unlink()
        model_blob.mkdir()
    elif change == "manifest":
        path.write_bytes(path.read_bytes() + b" ")
    else:
        weight = "b" * 64
    with pytest.raises(local_store.LocalStoreError) as exc:
        observe(root, manifest, weight)
    assert exc.value.state == state
    assert str(root) not in str(exc.value)


@pytest.mark.parametrize("change", ["duplicate", "oversized", "no_model", "two_models",
                                    "bool_size", "deep"])
def test_rejects_malformed_manifest(tmp_path, change):
    root, _, weight = store_fixture(tmp_path)
    path = root / local_store.manifest_relative_path("szl-khipu:latest")
    manifest = json.loads(path.read_text())
    if change == "duplicate":
        raw = path.read_bytes().replace(b'"schemaVersion":2',
                                         b'"schemaVersion":2,"schemaVersion":2')
    elif change == "oversized":
        raw = path.read_bytes() + b" " * local_store.MAX_MANIFEST_BYTES
    else:
        if change == "no_model":
            manifest["layers"] = manifest["layers"][1:]
        elif change == "two_models":
            manifest["layers"].append(manifest["layers"][0])
        elif change == "bool_size":
            manifest["layers"][0]["size"] = True
        else:
            nested = 0
            for _ in range(local_store.MAX_MANIFEST_DEPTH + 2):
                nested = [nested]
            manifest["extra"] = nested
        raw = json.dumps(manifest, separators=(",", ":")).encode()
    path.write_bytes(raw)
    with pytest.raises(local_store.LocalStoreError) as exc:
        observe(root, sha(raw), weight)
    assert exc.value.state in {"LOCAL_STORE_MANIFEST_INVALID", "LOCAL_STORE_SIZE_INVALID"}


def test_rejects_symlinked_blob(tmp_path):
    root, manifest, weight = store_fixture(tmp_path)
    path = root / "blobs" / ("sha256-" + weight)
    target = tmp_path / "elsewhere"
    target.write_bytes(path.read_bytes())
    path.unlink()
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable on this host")
    with pytest.raises(local_store.LocalStoreError) as exc:
        observe(root, manifest, weight)
    assert exc.value.state == "LOCAL_STORE_PATH_INVALID"


def test_check_has_time_budget(tmp_path, monkeypatch):
    root, manifest, weight = store_fixture(tmp_path)
    monkeypatch.setattr(local_store, "HASH_BUDGET_SECONDS", -1.0)
    with pytest.raises(local_store.LocalStoreError) as exc:
        observe(root, manifest, weight)
    assert exc.value.state == "LOCAL_STORE_CHECK_TIMEOUT"


def configure(monkeypatch, root, manifest, weight):
    registry = {"providers": [
        {"id": "local", "provider_type": "ollama_loopback",
         "models": {"szl-local": "szl-khipu:latest"},
         "model_digests": {"szl-local": manifest},
         "model_weight_digests": {"szl-local": weight}, "priority": 0,
         "enabled": True},
        {"id": "remote", "base_url": "https://remote.example.test/v1",
         "models": {"szl-local": "remote-model"}, "token_env": "REMOTE_TOKEN",
         "priority": 10, "enabled": True},
    ]}
    monkeypatch.setenv("SZL_ROUTER_PROVIDERS_JSON", json.dumps(registry))
    monkeypatch.setenv("SZL_ROUTER_OLLAMA_MODELS_DIR", str(root))
    monkeypatch.setenv("SZL_ROUTER_ALLOWED_HOSTS", "remote.example.test")
    monkeypatch.setenv("SZL_ROUTER_ENABLE_EGRESS", "1")
    monkeypatch.setenv("SZL_ROUTER_TOKEN", "test-router-client")
    monkeypatch.setenv("REMOTE_TOKEN", "test-remote-secret")


@pytest.mark.parametrize("change", ["none", "corrupt_weight", "missing_root", "timeout"])
def test_opted_in_completion_refuses_bad_bytes_without_cloud_fallback(tmp_path, monkeypatch, change):
    root, manifest, weight = store_fixture(tmp_path)
    configure(monkeypatch, root, manifest, weight)
    if change == "corrupt_weight":
        (root / "blobs" / ("sha256-" + weight)).write_bytes(b"tiny GGUF fixturE")
    elif change == "missing_root":
        monkeypatch.setenv("SZL_ROUTER_OLLAMA_MODELS_DIR", str(tmp_path / "missing"))
    elif change == "timeout":
        monkeypatch.setattr(local_store, "HASH_BUDGET_SECONDS", -1.0)
    calls = []
    generated = False

    async def inventory(kind):
        return {"szl-khipu:latest": manifest} if kind == "tags" or generated else {}

    async def provider_call(provider, payload):
        nonlocal generated
        calls.append(provider.id)
        assert provider.id == "local", "admission refusal reached remote provider"
        generated = True
        return {"model": "szl-khipu:latest", "choices": [{
            "message": {"role": "assistant", "content": "local answer"},
        }]}, 200

    monkeypatch.setattr(router, "ollama_inventory", inventory)
    monkeypatch.setattr(router, "call_provider", provider_call)
    client = TestClient(router.app, headers={"Authorization": "Bearer test-router-client"})
    status = client.get("/api/local-models").json()
    assert status["providers"][0]["models"][0]["store_byte_state"] == "PIN_CONFIGURED_NOT_PROBED"
    response = client.post("/v1/chat/completions", json={
        "model": "szl-local", "messages": [{"role": "user", "content": "fixture"}],
    })
    if change == "none":
        assert response.status_code == 200
        identity = response.json()["szl_receipt"]["model_identity"]
        assert identity["local_store_bytes"]["state"] == "LOCAL_STORE_BYTES_PRE_REQUEST_MATCH"
        assert identity["local_store_bytes"]["weight_sha256"] == weight
        assert identity["independent_attestation"] == "UNAVAILABLE"
        assert identity["license_state"] == "NOT_VERIFIED"
        assert calls == ["local"]
    else:
        assert response.status_code == 503
        assert response.json()["detail"]["state"] == "LOCAL_MODEL_IDENTITY_UNAVAILABLE"
        assert calls == []
        assert "test-remote-secret" not in response.text
        assert str(root) not in response.text


def test_opted_in_local_attempt_deadline_cannot_fall_through_to_cloud(tmp_path, monkeypatch):
    root, manifest, weight = store_fixture(tmp_path)
    configure(monkeypatch, root, manifest, weight)
    calls = []

    async def timed_out_attempt(provider, request, upstream_model):
        calls.append(provider.id)
        raise asyncio.TimeoutError

    monkeypatch.setattr(router, "attempt_candidate", timed_out_attempt)
    response = TestClient(router.app, headers={
        "Authorization": "Bearer test-router-client",
    }).post("/v1/chat/completions", json={
        "model": "szl-local", "messages": [{"role": "user", "content": "fixture"}],
    })
    assert response.status_code == 503
    assert response.json()["detail"]["state"] == "LOCAL_ROUTE_UNAVAILABLE"
    assert response.json()["detail"]["attempts"] == [{
        "provider_id": "local", "state": "LOCAL_ROUTE_DEADLINE_EXCEEDED",
    }]
    assert calls == ["local"]


@pytest.mark.parametrize("change", ["unknown_alias", "bad_pin", "remote_pin",
                                    "missing_root", "relative_root", "path_alias"])
def test_invalid_byte_admission_config_fails_closed(tmp_path, monkeypatch, change):
    root, manifest, weight = store_fixture(tmp_path)
    configure(monkeypatch, root, manifest, weight)
    registry = json.loads(os.environ["SZL_ROUTER_PROVIDERS_JSON"])
    local = registry["providers"][0]
    if change == "unknown_alias":
        local["model_weight_digests"] = {"other": weight}
    elif change == "bad_pin":
        local["model_weight_digests"] = {"szl-local": weight.upper()}
    elif change == "remote_pin":
        registry["providers"][1]["model_weight_digests"] = {"szl-local": weight}
    elif change == "missing_root":
        monkeypatch.delenv("SZL_ROUTER_OLLAMA_MODELS_DIR")
    elif change == "relative_root":
        monkeypatch.setenv("SZL_ROUTER_OLLAMA_MODELS_DIR", "relative/path")
    else:
        local["models"] = {"szl-local": "../elsewhere:latest"}
    monkeypatch.setenv("SZL_ROUTER_PROVIDERS_JSON", json.dumps(registry))
    assert TestClient(router.app).get("/readyz").json()["registry_state"] == "INVALID_FAIL_CLOSED"
