"""Network-free acceptance tests; real inference is the separate demo command."""
import importlib.util
import hashlib
import json
from pathlib import Path
import httpx
import pytest
from szl_router import core, receipts
import szl_receipt

spec = importlib.util.spec_from_file_location("local_demo", Path(__file__).parent / "demo/run_demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


def response(model="fixture:latest", binding_model=None, signed=True,
             backend_root="http://127.0.0.1:11434"):
    messages = [{"role": "user", "content": "fixture"}]
    route = "local_ollama:" + model
    body = {"model": model, "choices": [{"message": {"content": "fixture answer"}}],
            "x_szl_provenance": {"served_by": route, "base_url": backend_root + "/v1"}}
    priv, pub = szl_receipt.generate_keypair()
    payload = receipts.build_body(provenance=body["x_szl_provenance"], model=route,
        usage=None, req_digest=receipts.request_digest(binding_model or route, messages))
    envelope = szl_receipt.sign_receipt(szl_receipt.Receipt("inference.route", payload), priv if signed else None)
    resp = httpx.Response(200, json=body, headers={"x-szl-receipt": receipts.encode_header(envelope)},
                         request=httpx.Request("POST", "http://testserver"))
    return resp, messages, pub


def test_local_route_is_fixed_keyless_and_has_no_cloud_fallback(monkeypatch):
    provider = core.PROVIDERS["local_ollama"]
    assert provider.base_url() == "http://127.0.0.1:11434/v1"
    assert not provider.key_env and not provider.base_url_env
    monkeypatch.delenv("SZL_LOCAL_OLLAMA_ENABLE", raising=False)
    assert not provider.available()
    monkeypatch.setenv("SZL_LOCAL_OLLAMA_ENABLE", "1")
    assert provider.available()
    assert core.resolve_routes("local_ollama:fixture:latest") == [("local_ollama", "fixture:latest")]


def test_signed_fixture_passes_and_checks_tamper_rejection():
    resp, messages, pub = response()
    demo.check_response(resp, "fixture:latest", messages, pub)


@pytest.mark.parametrize("case", ["wrong-model", "wrong-request", "unsigned", "empty", "remote"])
def test_response_failures_are_not_green(case):
    resp, messages, pub = response(binding_model="wrong" if case == "wrong-request" else None,
                                   signed=case != "unsigned")
    body = resp.json()
    if case == "wrong-model":
        body["model"] = "wrong"
    if case == "empty":
        body["choices"][0]["message"]["content"] = ""
    if case == "remote":
        body["x_szl_provenance"]["base_url"] = "https://example.invalid/v1"
    changed = httpx.Response(200, json=body, headers=resp.headers, request=resp.request)
    with pytest.raises(ValueError):
        demo.check_response(changed, "fixture:latest", messages, pub)


def test_metadata_redirect_is_refused():
    with pytest.raises(ValueError):
        demo.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.invalid")


def test_uninstalled_model_fails_without_download(monkeypatch):
    monkeypatch.setattr(demo, "local_json", lambda *args: {"models": []})
    with pytest.raises(ValueError, match="not installed"):
        demo.model_identity("missing:latest")


def test_verifier_rejects_replaced_report_and_empty_evidence(tmp_path):
    raw = json.dumps({"schema": "szl.router-local-demo/v1", "requested_requests": 0}).encode()
    (tmp_path / "report.json").write_bytes(raw)
    with pytest.raises(ValueError, match="digest"):
        demo.verify(tmp_path, "0" * 64)
    with pytest.raises(ValueError, match="schema/count"):
        demo.verify(tmp_path, hashlib.sha256(raw).hexdigest())


@pytest.mark.parametrize("root", ["https://127.0.0.1:1234", "http://localhost:1234",
    "http://example.invalid:1234", "http://127.0.0.1:1234/path", "http://user@127.0.0.1:1234",
    "http://127.0.0.1:1234?x=1", "http://127.0.0.1:1234#x", "http://127.0.0.1:0"])
def test_worker_root_cannot_escape_fixed_loopback(root):
    with pytest.raises(ValueError):
        demo.loopback_root(root)


def test_worker_response_must_match_owned_port():
    resp, messages, pub = response(backend_root="http://127.0.0.1:12345")
    demo.check_response(resp, "fixture:latest", messages, pub, "http://127.0.0.1:12345")
    with pytest.raises(ValueError, match="loopback"):
        demo.check_response(resp, "fixture:latest", messages, pub, "http://127.0.0.1:12346")


def test_cpu_replay_requires_residency_and_cleanup_evidence(tmp_path):
    resp, messages, pub = response(backend_root="http://127.0.0.1:12345")
    row = {"index": 0, "response": resp.json(), "messages": messages,
           "envelope": receipts.decode_header(resp.headers["x-szl-receipt"]),
           "resident_model": {"name": "fixture:latest", "digest": "a" * 64, "size_vram": 0}}
    (tmp_path / "session.pub").write_bytes(pub)
    (tmp_path / "requests.jsonl").write_text(json.dumps(row) + "\n")
    report = {"schema": "szl.router-local-demo/v1", "requested_requests": 1,
              "completed_requests": 1, "status": "PASS", "model": {
                  "name": "fixture:latest", "manifest_sha256": "a" * 64},
              "backend_root": "http://127.0.0.1:12345", "worker": {"mode": "OWNED_CPU_PROCESS"},
              "worker_cleanup": "OWNED_PROCESS_TREE_CLOSED"}
    def write_report():
        report["artifact_sha256"] = {name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
                                     for name in ("session.pub", "requests.jsonl")}
        raw = (json.dumps(report) + "\n").encode()
        (tmp_path / "report.json").write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()
    assert demo.verify(tmp_path, write_report())["verified_receipts"] == 1
    row["resident_model"]["size_vram"] = 99
    (tmp_path / "requests.jsonl").write_text(json.dumps(row) + "\n")
    with pytest.raises(ValueError, match="residency/cleanup"):
        demo.verify(tmp_path, write_report())
