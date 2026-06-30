"""Signed-receipt contract tests for SZL Router (offline, no real API keys).

Proves the doctrine end to end:
  * a routed /v1/chat/completions answer carries the `x-szl-receipt` header AND
    keeps the existing `x_szl_provenance` body block;
  * with a signing key the receipt verifies via szl_receipt.verify_receipt;
  * tampering with the receipt body fails verification;
  * a wrong public key fails verification;
  * KEYLESS = UNSIGNED-honest — verify returns (False,"unsigned-honest"), never a
    fabricated pass;
  * the /v1/receipt/verify endpoint and the verify CLI return the same verdict.

Upstreams are MOCKED exactly like the existing suite (we monkeypatch the core
HTTP poster), so nothing here needs network or real keys.

Run: python3 test_signed_receipt.py   (also collected by pytest)
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, "szl_router")

import pytest  # noqa: E402

szl_receipt = pytest.importorskip(
    "szl_receipt",
    reason="szl-receipt not installed; install the 'sign' extra to run signing tests",
)

# Use the SAME core module instance the app imports (szl_router.core), so the
# upstream mock applied here is the one chat() actually calls.
from szl_router import core  # noqa: E402
from szl_router import receipts as R  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers: arm a fake sovereign provider + mock the upstream so a route answers
# without any network or real key.
# ---------------------------------------------------------------------------
_FAKE_COMPLETION = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ROUTER_OK"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
}


@pytest.fixture()
def armed_router(monkeypatch):
    # Arm box_gpu (a sovereign provider) purely via env, and mock its HTTP call.
    monkeypatch.setenv("A11OY_MODEL_BASE_URL", "http://fake-gpu.local:11434/v1")
    monkeypatch.setenv("A11OY_GPU_TOKEN", "test-token")

    def fake_post_chat(provider, payload, timeout):
        return dict(_FAKE_COMPLETION)

    monkeypatch.setattr(core, "_post_chat", fake_post_chat)
    return core


def _fresh_signing(monkeypatch, **env):
    """Reset the one-shot signing state and re-init under the given env."""
    for k in ("SZL_RECEIPT_KEY_PEM", "SZL_RECEIPT_KEY_FILE", "SZL_RECEIPT_EPHEMERAL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(R, "_INITIALIZED", False)
    monkeypatch.setattr(R, "_STATE", R._KeyState())
    R.init_signing(log=lambda *_a, **_k: None)


def _client():
    from starlette.testclient import TestClient
    from szl_router.app import app
    return TestClient(app)


# ---------------------------------------------------------------------------
# 1. Routed answer carries x-szl-receipt AND keeps x_szl_provenance; signed
#    receipt verifies against the session public key.
# ---------------------------------------------------------------------------
def test_routed_response_has_signed_receipt(monkeypatch, armed_router):
    priv, pub = szl_receipt.generate_keypair()
    _fresh_signing(monkeypatch, SZL_RECEIPT_KEY_PEM=priv.decode("ascii"))

    with _client() as client:
        resp = client.post("/v1/chat/completions",
                           json={"model": "szl-large",
                                 "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    data = resp.json()
    assert "x_szl_provenance" in data            # body block preserved
    assert data["x_szl_provenance"]["sovereign"] is True

    hdr = resp.headers.get("x-szl-receipt")
    assert hdr, "response must carry the x-szl-receipt header"
    env = R.decode_header(hdr)
    assert env["signed"] is True
    assert env["organ"] == "szl-router"

    ok, detail = szl_receipt.verify_receipt(env, public_key_pem=pub)
    assert ok and detail == "ok"

    body = json.loads(__import__("base64").b64decode(env["payload"]))
    assert body["served_by"] == "box_gpu:llama3.1:8b"
    assert body["sovereign"] is True
    assert body["tier"] == "sovereign"
    assert body["usage"]["total_tokens"] == 5
    assert len(body["request_digest"]) == 64


# ---------------------------------------------------------------------------
# 2. Tampering with the body fails verification.
# ---------------------------------------------------------------------------
def test_tampered_body_fails_verification(monkeypatch, armed_router):
    import base64
    priv, pub = szl_receipt.generate_keypair()
    _fresh_signing(monkeypatch, SZL_RECEIPT_KEY_PEM=priv.decode("ascii"))

    with _client() as client:
        resp = client.post("/v1/chat/completions",
                           json={"model": "szl-large",
                                 "messages": [{"role": "user", "content": "hi"}]})
    env = R.decode_header(resp.headers["x-szl-receipt"])

    body = json.loads(base64.b64decode(env["payload"]))
    body["sovereign"] = False  # forge a non-sovereign claim
    env["payload"] = base64.b64encode(
        json.dumps(body, separators=(",", ":")).encode()).decode("ascii")

    ok, detail = szl_receipt.verify_receipt(env, public_key_pem=pub)
    assert not ok and detail == "signature mismatch"


# ---------------------------------------------------------------------------
# 3. Wrong public key fails verification.
# ---------------------------------------------------------------------------
def test_wrong_key_fails_verification(monkeypatch, armed_router):
    priv, _pub = szl_receipt.generate_keypair()
    _, other_pub = szl_receipt.generate_keypair()
    _fresh_signing(monkeypatch, SZL_RECEIPT_KEY_PEM=priv.decode("ascii"))

    with _client() as client:
        resp = client.post("/v1/chat/completions",
                           json={"model": "szl-large",
                                 "messages": [{"role": "user", "content": "hi"}]})
    env = R.decode_header(resp.headers["x-szl-receipt"])
    ok, _detail = szl_receipt.verify_receipt(env, public_key_pem=other_pub)
    assert not ok


# ---------------------------------------------------------------------------
# 4. KEYLESS = UNSIGNED-honest (never a fabricated signature).
# ---------------------------------------------------------------------------
def test_keyless_is_unsigned_honest(monkeypatch, armed_router):
    _fresh_signing(monkeypatch, SZL_RECEIPT_EPHEMERAL="0")  # truly keyless

    with _client() as client:
        resp = client.post("/v1/chat/completions",
                           json={"model": "szl-large",
                                 "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    env = R.decode_header(resp.headers["x-szl-receipt"])
    assert env["signed"] is False
    assert "UNSIGNED-honest" in env.get("note", "")

    ok, detail = szl_receipt.verify_receipt(env, public_key_pem=None)
    assert not ok and detail == "unsigned-honest"


# ---------------------------------------------------------------------------
# 5. First-boot ephemeral key: signs, and its public key is exposed honestly.
# ---------------------------------------------------------------------------
def test_ephemeral_session_key_signs(monkeypatch, armed_router):
    _fresh_signing(monkeypatch)  # no key + ephemeral default on
    assert R.signing_state().ephemeral is True

    with _client() as client:
        resp = client.post("/v1/chat/completions",
                           json={"model": "szl-large",
                                 "messages": [{"role": "user", "content": "hi"}]})
        env = R.decode_header(resp.headers["x-szl-receipt"])
        assert env["signed"] is True

        pk = client.get("/v1/receipt/pubkey").json()
        assert pk["ephemeral"] is True
        assert pk["public_key_pem"].startswith("-----BEGIN PUBLIC KEY-----")

    ok, _ = szl_receipt.verify_receipt(
        env, public_key_pem=pk["public_key_pem"].encode("ascii"))
    assert ok


# ---------------------------------------------------------------------------
# 6. /v1/receipt/verify endpoint returns the same verdict (signed + tampered).
# ---------------------------------------------------------------------------
def test_verify_endpoint(monkeypatch, armed_router):
    priv, pub = szl_receipt.generate_keypair()
    _fresh_signing(monkeypatch, SZL_RECEIPT_KEY_PEM=priv.decode("ascii"))

    with _client() as client:
        resp = client.post("/v1/chat/completions",
                           json={"model": "szl-large",
                                 "messages": [{"role": "user", "content": "hi"}]})
        hdr = resp.headers["x-szl-receipt"]

        good = client.post("/v1/receipt/verify",
                          json={"envelope": hdr,
                                "public_key_pem": pub.decode("ascii")}).json()
        assert good == {"valid": True, "detail": "ok", "signed": True}

        env = R.decode_header(hdr)
        env["digest"] = "0" * 64  # superficial poke; signature still over payload
        import base64
        body = json.loads(base64.b64decode(env["payload"]))
        body["model"] = "forged"
        env["payload"] = base64.b64encode(
            json.dumps(body, separators=(",", ":")).encode()).decode("ascii")
        bad = client.post("/v1/receipt/verify",
                         json={"envelope": env,
                               "public_key_pem": pub.decode("ascii")}).json()
        assert bad["valid"] is False


# ---------------------------------------------------------------------------
# 7. verify CLI agrees (scriptable for buyers).
# ---------------------------------------------------------------------------
def test_verify_cli(monkeypatch, tmp_path, armed_router, capsys):
    priv, pub = szl_receipt.generate_keypair()
    _fresh_signing(monkeypatch, SZL_RECEIPT_KEY_PEM=priv.decode("ascii"))

    with _client() as client:
        resp = client.post("/v1/chat/completions",
                           json={"model": "szl-large",
                                 "messages": [{"role": "user", "content": "hi"}]})
        hdr = resp.headers["x-szl-receipt"]

    pub_path = tmp_path / "session.pub"
    pub_path.write_bytes(pub)
    from szl_router import verify as V

    rc = V.main(["--envelope", hdr, "--pubkey", str(pub_path)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["valid"] is True and out["detail"] == "ok"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
