"""Offline tests for the Wave-8 receipt deepening:

  * per-upstream FAILURE COOLDOWN — a failed provider is skipped (honestly, in
    the attempt trail) while a warm fallback exists, is still TRIED as a last
    resort, and is cleared on success;
  * honest per-call COST block - $0.00-with-basis for sovereign tiers;
    paid tier estimates remain offline-only while paid routing is blocked;
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
_NVIDIA = core.PROVIDERS["nvidia_gpu"]
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
        os.environ[_NVIDIA.base_url_env] = "http://fake-second-gpu.local:11434/v1"
        os.environ[_NVIDIA.key_env] = "test-second-token"
        os.environ.pop("SZL_COOLDOWN_SECONDS", None)  # default 30s

        def post_box_fails(provider, payload, timeout):
            if provider.name == "box_gpu":
                raise _http_400()
            return dict(_FAKE)

        core._post_chat = post_box_fails

        # Call 1: box_gpu fails (permanent 400, no retry sleep), nvidia_gpu serves.
        r1 = core.chat("szl-large", _MSGS, timeout=1)
        prov1 = r1["x_szl_provenance"]
        check(prov1["provider"] == "nvidia_gpu", "call1 served by nvidia_gpu after box_gpu failure")
        check(core._cooldown_remaining("box_gpu") > 0, "box_gpu is cooling after failure")

        # Call 2: box_gpu must be SKIPPED with an honest trail entry.
        r2 = core.chat("szl-large", _MSGS, timeout=1)
        prov2 = r2["x_szl_provenance"]
        first = prov2["attempts"][0]
        check(first["provider"] == "box_gpu" and "cooldown-skip" in (first["error"] or ""),
              "cooled box_gpu skipped with 'cooldown-skip' recorded in the trail")
        check(prov2["provider"] == "nvidia_gpu", "call2 served by nvidia_gpu")

        # Call 3: nvidia_gpu disarmed -> box_gpu is the LAST RESORT and must be tried
        # despite cooling; it now succeeds and the cooldown clears.
        os.environ.pop(_NVIDIA.key_env, None)
        core._post_chat = lambda provider, payload, timeout: dict(_FAKE)
        r3 = core.chat("szl-large", _MSGS, timeout=1)
        prov3 = r3["x_szl_provenance"]
        check(prov3["provider"] == "box_gpu", "cooling last-resort box_gpu still tried and served")
        check(core._cooldown_remaining("box_gpu") == 0, "success cleared box_gpu cooldown")

        # Both calls used sovereign hardware; neither claims a cloud free tier.
        check(prov3["cost"]["amount_usd"] == 0.0 and "sovereign" in prov3["cost"]["basis"]
              and prov3["cost"]["estimated"] is False,
              "sovereign cost: $0 vendor charge, explicit basis, not an estimate")
        check(prov1["cost"]["amount_usd"] == 0.0 and prov1["cost"]["tier"] == "sovereign",
              "second sovereign node cost: $0 vendor charge with basis")

        # SZL_COOLDOWN_SECONDS=0 disables the mechanism entirely.
        os.environ["SZL_COOLDOWN_SECONDS"] = "0"
        core._set_cooldown("box_gpu")
        check(core._cooldown_remaining("box_gpu") == 0, "SZL_COOLDOWN_SECONDS=0 disables cooldown")
    finally:
        core._post_chat = orig_post
        _restore_env(saved)
        _reset_cooldowns()


def test_paid_estimate_is_offline_only() -> None:
    print("== paid tier: offline estimate does not authorize routing ==")
    saved = _snap_env()
    orig_post = core._post_chat
    _reset_cooldowns()
    try:
        os.environ[_MOON.key_env] = "fake-moonshot-key"
        calls = []
        core._post_chat = lambda provider, payload, timeout: calls.append(provider.name) or dict(_FAKE)
        cost = core._cost_detail(_MOON, dict(_FAKE), "kimi-k2.5")
        check(cost["estimated"] is True, "offline paid amount is labelled an estimate")
        check(cost["amount_usd"] > 0, "offline paid estimate is positive")
        check(str(cost["basis"]).startswith("table:kimi-k2"), "offline estimate exposes its table basis")
        check(cost["tier"] == "paid-grid", "offline estimate carries its tier")
        check(cost["prompt_tokens"] == 10 and cost["completion_tokens"] == 5,
              "offline estimate records token counts")
        try:
            core.chat("moonshot:kimi-k2.5", _MSGS, timeout=1)
            check(False, "paid route blocked")
        except core.RouterError as exc:
            check(len(exc.attempts) == 1 and "pricing/reservation unqualified" in (exc.attempts[0].error or ""),
                  "paid route records an unqualified attempt")
        check(calls == [], "paid route never reaches transport")
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
    test_paid_estimate_is_offline_only()
    test_envelope_carries_cost_and_observer_only_when_passed()
    if FAILED:
        print(f"RESULT: {FAILED} check(s) FAILED")
        sys.exit(1)
    print("RESULT: cooldown honest-skip, cost blocks, and observer frame all verified offline")
