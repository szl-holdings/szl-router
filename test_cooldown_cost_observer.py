"""Offline tests for the Wave-8 receipt deepening:

  * per-upstream FAILURE COOLDOWN — a failed provider is skipped (honestly, in
    the attempt trail) while a warm fallback exists, is still TRIED as a last
    resort, and is cleared on success;
  * honest per-call COST block — $0.00-with-basis for free/sovereign tiers,
    the spend-guard's labelled ESTIMATE for paid tiers;
  * OBSERVER frame + cost land in the receipt envelope ONLY when passed
    (older callers stay byte-identical).

NO network: upstreams are stubbed by monkeypatching core._post_chat exactly
like the existing suites; no real sleeping (permanent 400s skip the backoff).

Run: python3 test_cooldown_cost_observer.py   (also collected by pytest)
"""
from __future__ import annotations

import io
import os
import sys
import urllib.error

sys.path.insert(0, "szl_router")
import core  # noqa: E402

FAILED = 0


def check(cond: bool, msg: str) -> None:
    global FAILED
    if cond:
        print("  OK  " + msg)
    else:
        FAILED += 1
        print("  BAD " + msg)
    assert cond, msg


_FAKE = {
    "id": "chatcmpl-test", "object": "chat.completion",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}
_MSGS = [{"role": "user", "content": "hi"}]

_BOX = core.PROVIDERS["box_gpu"]
_GROQ = core.PROVIDERS["groq"]
_MOON = core.PROVIDERS["moonshot"]

# HERMETIC: every provider's arming env var, so real keys in the host
# environment (e.g. a developer's OPENROUTER_API_KEY) can never leak into the
# routing chain under test.
_ALL_PROVIDER_ENVS = {e for p in core.PROVIDERS.values()
                      for e in (p.base_url_env, p.key_env) if e}
_ENV_KEYS = tuple(_ALL_PROVIDER_ENVS |
                  {"SZL_COOLDOWN_SECONDS", "SZL_SPEND_LEDGER_FILE",
                   "SZL_SPEND_KILL_FILE"})


def _snap_env():
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    for k in _ALL_PROVIDER_ENVS:
        os.environ.pop(k, None)  # start from a fully-disarmed provider fleet
    return saved


def _restore_env(saved) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _reset_cooldowns() -> None:
    with core._COOLDOWN_LOCK:
        core._COOLDOWN_UNTIL.clear()


def _http_400() -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x/v1/chat/completions", 400, "boom",
                                  {}, io.BytesIO(b'{"error":"boom"}'))


def test_cooldown_skip_then_last_resort_then_clear() -> None:
    print("== cooldown: honest skip with warm fallback; last resort still tried; success clears ==")
    saved = _snap_env()
    orig_post = core._post_chat
    _reset_cooldowns()
    try:
        os.environ[_BOX.base_url_env] = "http://fake-gpu.local:11434/v1"
        os.environ[_BOX.key_env] = "test-token"
        os.environ[_GROQ.key_env] = "fake-groq-key"
        os.environ.pop("SZL_COOLDOWN_SECONDS", None)  # default 30s

        def post_box_fails(provider, payload, timeout):
            if provider.name == "box_gpu":
                raise _http_400()
            return dict(_FAKE)

        core._post_chat = post_box_fails

        # Call 1: box_gpu fails (permanent 400, no retry sleep), groq serves.
        r1 = core.chat("szl-large", _MSGS, timeout=1)
        prov1 = r1["x_szl_provenance"]
        check(prov1["provider"] == "groq", "call1 served by groq after box_gpu failure")
        check(core._cooldown_remaining("box_gpu") > 0, "box_gpu is cooling after failure")

        # Call 2: box_gpu must be SKIPPED with an honest trail entry.
        r2 = core.chat("szl-large", _MSGS, timeout=1)
        prov2 = r2["x_szl_provenance"]
        first = prov2["attempts"][0]
        check(first["provider"] == "box_gpu" and "cooldown-skip" in (first["error"] or ""),
              "cooled box_gpu skipped with 'cooldown-skip' recorded in the trail")
        check(prov2["provider"] == "groq", "call2 served by groq")

        # Call 3: groq disarmed -> box_gpu is the LAST RESORT and must be tried
        # despite cooling; it now succeeds and the cooldown clears.
        os.environ.pop(_GROQ.key_env, None)
        core._post_chat = lambda provider, payload, timeout: dict(_FAKE)
        r3 = core.chat("szl-large", _MSGS, timeout=1)
        prov3 = r3["x_szl_provenance"]
        check(prov3["provider"] == "box_gpu", "cooling last-resort box_gpu still tried and served")
        check(core._cooldown_remaining("box_gpu") == 0, "success cleared box_gpu cooldown")

        # Honest cost blocks: sovereign metal on call 3, free tier on call 1.
        check(prov3["cost"]["amount_usd"] == 0.0 and "sovereign" in prov3["cost"]["basis"]
              and prov3["cost"]["estimated"] is False,
              "sovereign cost: $0 vendor charge, explicit basis, not an estimate")
        check(prov1["cost"]["amount_usd"] == 0.0 and "free-tier" in prov1["cost"]["basis"],
              "free-tier cost: $0 vendor charge with basis")

        # SZL_COOLDOWN_SECONDS=0 disables the mechanism entirely.
        os.environ["SZL_COOLDOWN_SECONDS"] = "0"
        core._set_cooldown("box_gpu")
        check(core._cooldown_remaining("box_gpu") == 0, "SZL_COOLDOWN_SECONDS=0 disables cooldown")
    finally:
        core._post_chat = orig_post
        _restore_env(saved)
        _reset_cooldowns()


def test_paid_cost_is_labelled_estimate() -> None:
    print("== paid tier: cost is the spend-guard's labelled estimate ==")
    saved = _snap_env()
    orig_post = core._post_chat
    _reset_cooldowns()
    try:
        os.environ[_MOON.key_env] = "fake-moonshot-key"
        os.environ["SZL_SPEND_LEDGER_FILE"] = "/tmp/test-szl-spend-ledger.jsonl"
        os.environ["SZL_SPEND_KILL_FILE"] = "/tmp/test-szl-spend-KILL-absent"
        try:
            os.remove("/tmp/test-szl-spend-ledger.jsonl")
        except FileNotFoundError:
            pass
        core._post_chat = lambda provider, payload, timeout: dict(_FAKE)
        r = core.chat("moonshot:kimi-k2.5", _MSGS, timeout=1)
        cost = r["x_szl_provenance"]["cost"]
        check(cost["estimated"] is True, "paid cost is labelled estimated:true")
        check(cost["amount_usd"] > 0, "paid cost amount > 0")
        check(str(cost["basis"]).startswith("table:kimi-k2"), "paid rate basis is auditable (price table)")
        check(cost["tier"] == "paid-grid", "paid cost carries its tier")
        check(cost["prompt_tokens"] == 10 and cost["completion_tokens"] == 5,
              "paid cost records the token counts it priced")
    finally:
        core._post_chat = orig_post
        _restore_env(saved)
        _reset_cooldowns()


def test_envelope_carries_cost_and_observer_only_when_passed() -> None:
    print("== envelope: cost + observer additive, absent for older callers ==")
    try:
        import szl_receipt  # noqa: F401
    except Exception:
        print("  SKIP szl-receipt not installed; envelope shape checked in test_signed_receipt.py env")
        return
    import base64
    import json as _json
    import receipts as R  # szl_router on sys.path

    R._INITIALIZED = False
    R._STATE = R._KeyState()
    R.init_signing(log=lambda *a, **k: None)

    prov = {"served_by": "box_gpu:m", "sovereign": True, "tier": "sovereign",
            "energy_source": "self-hosted", "attempts": []}
    env_with = R.build_envelope(provenance=prov, model="m", usage=None,
                                req_digest="a" * 64,
                                cost={"amount_usd": 0.0, "basis": "sovereign-owned-metal",
                                      "estimated": False},
                                observer={"endpoint": "/v1/chat/completions",
                                          "auth_mode": "open", "requested_model": "m"})
    body = _json.loads(base64.b64decode(env_with["payload"]))
    check(body.get("cost", {}).get("basis") == "sovereign-owned-metal",
          "cost block signed into envelope when passed")
    check(body.get("observer", {}).get("auth_mode") == "open",
          "observer frame signed into envelope when passed")

    env_without = R.build_envelope(provenance=prov, model="m", usage=None,
                                   req_digest="a" * 64)
    body2 = _json.loads(base64.b64decode(env_without["payload"]))
    check("cost" not in body2 and "observer" not in body2,
          "older callers' envelopes stay byte-identical (no cost/observer keys)")


if __name__ == "__main__":
    test_cooldown_skip_then_last_resort_then_clear()
    test_paid_cost_is_labelled_estimate()
    test_envelope_carries_cost_and_observer_only_when_passed()
    if FAILED:
        print(f"RESULT: {FAILED} check(s) FAILED")
        sys.exit(1)
    print("RESULT: cooldown honest-skip, cost blocks, and observer frame all verified offline")
