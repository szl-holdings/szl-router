"""Pure, offline, deterministic tests for the same-provider transient retry
(exponential backoff + full jitter) added to core.py.

NO network and NO real sleeping: the poster is an in-memory stub and time.sleep
is monkeypatched to record the backoff schedule. Validates that transient errors
(429/5xx + connection blips) are retried up to the budget, that permanent 4xx
errors are NOT retried, and that the jitter stays within the full-jitter ceiling.

Run: python3 test_retry_backoff.py   (also collected by pytest)
"""

from __future__ import annotations

import http.client
import io
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
    # assert so pytest actually gates on a bad check (standalone __main__ still
    # prints the full report first via the FAILED counter / exit code).
    assert cond, msg


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x/v1/chat/completions", code, "boom",
                                  {}, io.BytesIO(b'{"error":"boom"}'))


def _patch_sleep():
    """Replace core.time.sleep with a recorder; return (slept_list, restore)."""
    slept = []
    orig = core.time.sleep
    core.time.sleep = lambda s: slept.append(s)  # type: ignore
    return slept, (lambda: setattr(core.time, "sleep", orig))


def test_retries_transient_then_succeeds() -> None:
    print("== transient 503 then 200: retried, succeeds ==")
    slept, restore = _patch_sleep()
    try:
        calls = {"n": 0}

        def poster(provider, payload, timeout):
            calls["n"] += 1
            if calls["n"] < 3:
                raise _http_error(503)
            return {"ok": True, "choices": []}

        out = core._post_with_retry(poster, None, {}, 1.0)
        check(out == {"ok": True, "choices": []}, "returns the eventual success body")
        check(calls["n"] == 3, "called provider 3x (2 transient + 1 success)")
        check(len(slept) == 2, "slept between the 2 retries (not after the success)")
        check(all(s >= 0 for s in slept), "backoff sleeps are non-negative")
    finally:
        restore()
    print()


def test_permanent_4xx_not_retried() -> None:
    print("== permanent 400/401/404: NOT retried ==")
    for code in (400, 401, 404):
        slept, restore = _patch_sleep()
        try:
            calls = {"n": 0}

            def poster(provider, payload, timeout):
                calls["n"] += 1
                raise _http_error(code)

            raised = False
            try:
                core._post_with_retry(poster, None, {}, 1.0)
            except urllib.error.HTTPError as e:
                raised = e.code == code
            check(raised, f"{code} re-raised unchanged")
            check(calls["n"] == 1, f"{code} tried exactly once (no retry)")
            check(slept == [], f"{code} never slept/backed off")
        finally:
            restore()
    print()


def test_429_is_retried() -> None:
    print("== 429 rate-limit IS retried (transient) ==")
    slept, restore = _patch_sleep()
    try:
        calls = {"n": 0}

        def poster(provider, payload, timeout):
            calls["n"] += 1
            raise _http_error(429)

        raised = False
        try:
            core._post_with_retry(poster, None, {}, 1.0)
        except urllib.error.HTTPError as e:
            raised = e.code == 429
        check(raised, "429 re-raised after budget spent")
        check(calls["n"] == core._RETRY_MAX_ATTEMPTS,
              "429 retried up to the full attempt budget (%d)" % core._RETRY_MAX_ATTEMPTS)
    finally:
        restore()
    print()


def test_connection_blip_is_retried() -> None:
    print("== connection-level blip (OSError) IS retried ==")
    slept, restore = _patch_sleep()
    try:
        calls = {"n": 0}

        def poster(provider, payload, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                raise http.client.HTTPException("dropped")
            if calls["n"] == 2:
                raise OSError("connection reset")
            return {"choices": []}

        out = core._post_with_retry(poster, None, {}, 1.0)
        check(out == {"choices": []}, "succeeds after two connection blips")
        check(calls["n"] == 3, "tried 3x across HTTPException + OSError + success")
    finally:
        restore()
    print()


def test_full_jitter_within_ceiling() -> None:
    print("== full-jitter backoff stays within the exponential ceiling ==")
    inv = []
    for attempt in range(0, 6):
        ceiling = min(core._RETRY_MAX_DELAY, core._RETRY_BASE_DELAY * (2 ** attempt))
        for _ in range(200):
            s = core._backoff_sleep_seconds(attempt)
            if s < 0 or s > ceiling + 1e-9:
                inv.append((attempt, s, ceiling))
                break
    check(not inv, "every sampled sleep in [0, min(cap, base*2^attempt)] (full jitter)")
    check(core._backoff_sleep_seconds(99) <= core._RETRY_MAX_DELAY + 1e-9,
          "deep retries are capped at SZL_RETRY_MAX_DELAY")
    print()


if __name__ == "__main__":
    test_retries_transient_then_succeeds()
    test_permanent_4xx_not_retried()
    test_429_is_retried()
    test_connection_blip_is_retried()
    test_full_jitter_within_ceiling()
    if FAILED:
        print("\nRESULT: %d retry/backoff check(s) FAILED." % FAILED)
        sys.exit(1)
    print("\nRESULT: all retry/backoff checks passed (offline, deterministic).")
