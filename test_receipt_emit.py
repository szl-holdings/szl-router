"""Pure, offline tests for the fire-and-forget route receipt emitter in core.py.

The emitter must NEVER block routing and NEVER raise, even when the sink is
unreachable, and must be a no-op when SZL_RECEIPT_SINK is unset. The failure
path of chat() must still surface RouterError unchanged.

NO real network is required: an unreachable loopback port stands in for a dead
sink, and the daemon-thread send swallows the connection error.

Run: python3 test_receipt_emit.py   (also collected by pytest)
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, "szl_router")
import core  # noqa: E402

FAILED = 0


def _check(cond: bool, label: str) -> None:
    global FAILED
    if cond:
        print(f"  ok   {label}")
    else:
        FAILED += 1
        print(f"  FAIL {label}")


def _set_sink(value):
    if value is None:
        os.environ.pop("SZL_RECEIPT_SINK", None)
    else:
        os.environ["SZL_RECEIPT_SINK"] = value


def test_emit_noop_without_sink():
    saved = os.environ.get("SZL_RECEIPT_SINK")
    try:
        _set_sink(None)
        # No sink configured → must return immediately without raising.
        core._emit_route_receipt(model="szl-fast", decision="served",
                                 provenance=None, attempts=[])
        _check(True, "no-op when SZL_RECEIPT_SINK unset")
    except Exception as e:  # noqa: BLE001
        _check(False, f"no-op raised: {e!r}")
    finally:
        _set_sink(saved)


def test_emit_never_raises_with_unreachable_sink():
    saved = os.environ.get("SZL_RECEIPT_SINK")
    try:
        _set_sink("http://127.0.0.1:9")  # closed port
        core._emit_route_receipt(model="szl-fast", decision="served",
                                 provenance=None, attempts=[])
        time.sleep(0.2)  # let the daemon thread fail and swallow
        _check(True, "emitter swallows unreachable sink")
    except Exception as e:  # noqa: BLE001
        _check(False, f"emitter raised on unreachable sink: {e!r}")
    finally:
        _set_sink(saved)


def test_chat_failure_still_raises_with_unreachable_sink():
    saved = os.environ.get("SZL_RECEIPT_SINK")
    try:
        _set_sink("http://127.0.0.1:9")
        # With no providers armed, every route fails; the emission on the
        # failure path must not mask or alter the RouterError.
        try:
            core.chat("szl-fast", [{"role": "user", "content": "x"}], timeout=1)
            _check(False, "expected RouterError when no route is armed")
        except core.RouterError:
            _check(True, "RouterError still raised with unreachable sink")
        except Exception as e:  # noqa: BLE001
            _check(False, f"unexpected error type: {type(e).__name__}: {e}")
    finally:
        _set_sink(saved)


if __name__ == "__main__":
    test_emit_noop_without_sink()
    test_emit_never_raises_with_unreachable_sink()
    test_chat_failure_still_raises_with_unreachable_sink()
    if FAILED:
        print(f"RESULT: {FAILED} check(s) FAILED")
        sys.exit(1)
    print("RESULT: receipt emitter is fire-and-forget and non-blocking")
