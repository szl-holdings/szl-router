"""Strict request fingerprint: finite JSON before router/model dispatch."""

import asyncio
import hashlib
import json

import httpx
import pytest

from szl_router import app as router_app, receipts

MODEL = "szl-fast"
MESSAGES = [{"role": "user", "content": {"query": "public", "n": 3}}]


def _post(payload: bytes):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=router_app.app), base_url="http://test"
        ) as client:
            return await client.post(
                "/v1/chat/completions",
                content=payload,
                headers={"Content-Type": "application/json"},
            )

    return asyncio.run(run())


def test_finite_json_digest_is_legacy_byte_identical():
    old = json.dumps(
        {"model": MODEL, "messages": MESSAGES},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    assert (
        receipts.request_digest(MODEL, MESSAGES)
        == hashlib.sha256(old.encode()).hexdigest()
    )
    assert len(receipts.request_digest(MODEL, MESSAGES)) == 64


@pytest.mark.parametrize(
    "invalid", [float("nan"), float("inf"), -float("inf"), object(), b"bytes"]
)
def test_non_json_or_nonfinite_cannot_be_signed_as_request(invalid):
    with pytest.raises(ValueError, match="finite JSON"):
        receipts.request_digest(MODEL, [{"role": "user", "content": invalid}])


def test_request_model_is_not_python_stringified():
    with pytest.raises(ValueError, match="nonempty string"):
        receipts.request_digest(123, MESSAGES)
    with pytest.raises(ValueError, match="nonempty string"):
        receipts.request_digest("", MESSAGES)


def test_nested_change_changes_fingerprint():
    other = [{"role": "user", "content": {"query": "public", "n": 4}}]
    assert receipts.request_digest(MODEL, MESSAGES) != receipts.request_digest(
        MODEL, other
    )


@pytest.mark.parametrize("bad", ("NaN", "Infinity", "-Infinity"))
def test_invalid_json_number_rejected_before_any_provider_call(monkeypatch, bad):
    called = []
    monkeypatch.setenv("SZL_ROUTER_ALLOW_NO_AUTH", "1")
    monkeypatch.delenv("SZL_ROUTER_TOKEN", raising=False)
    monkeypatch.setattr(router_app.core, "chat", lambda *a, **k: called.append(1))
    body = (
        '{"model":"szl-fast","messages":[{"role":"user","content":' + bad + "}]}"
    ).encode()
    r = _post(body)
    assert r.status_code == 400
    assert called == []


def test_valid_request_digest_bound_once_before_dispatch(monkeypatch):
    seen = []
    monkeypatch.setenv("SZL_ROUTER_ALLOW_NO_AUTH", "1")
    monkeypatch.delenv("SZL_ROUTER_TOKEN", raising=False)
    monkeypatch.setattr(
        router_app.core,
        "chat",
        lambda *a, **k: (
            seen.append("dispatch")
            or {
                "choices": [],
                "x_szl_provenance": {},
            }
        ),
    )

    def capture_receipt(**kwargs):
        seen.append(kwargs["req_digest"])
        return None

    monkeypatch.setattr(router_app.receipts, "build_envelope", capture_receipt)
    r = _post(json.dumps({"model": MODEL, "messages": MESSAGES}).encode())
    assert r.status_code == 200
    assert seen == ["dispatch", receipts.request_digest(MODEL, MESSAGES)]
