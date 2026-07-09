"""Hermetic tests for the optional honest grid_context on SZL Router receipts.

No real network: every fetch is exercised through an INJECTED ``_transport``
(mock HTTP), exactly like szl-energy-attest PR #17. Proves:

  * a real UK Carbon Intensity payload maps to a REPORTED grid_context, verbatim;
  * any failure (raise / bad shape / missing intensity) -> honest UNAVAILABLE
    all-null block, never a fabricated number;
  * ``sanitize_grid_context`` coerces non-finite/junk numbers to null+UNAVAILABLE
    and forces labels to follow the value;
  * ``build_body`` carries grid_context when present and is BYTE-IDENTICAL to the
    legacy body when it is absent (back-compat), same as cost/observer;
  * ``current_grid_context`` NEVER blocks the request path and NEVER raises, and
    a background refresh eventually warms the cache.

Run: python3 test_grid_context.py   (also collected by pytest)
"""
from __future__ import annotations

import json
import sys
import time

sys.path.insert(0, "szl_router")

import grid  # noqa: E402
import receipts as R  # noqa: E402

FAILED = 0


def _check(cond: bool, label: str) -> None:
    global FAILED
    if cond:
        print(f"  ok   {label}")
    else:
        FAILED += 1
        print(f"  FAIL {label}")


# A real-shape UK Carbon Intensity API response (national /intensity).
# Source: https://api.carbonintensity.org.uk/  (docs: https://carbon-intensity.github.io/api-definitions/)
_UK_PAYLOAD = {
    "data": [{
        "from": "2026-07-09T18:30Z",
        "to": "2026-07-09T19:00Z",
        "intensity": {"forecast": 190, "actual": 173, "index": "moderate"},
    }]
}


def _transport_ok(url, headers, timeout):
    _check(url == grid.UK_CI_NATIONAL_URL, "transport hits the UK CI national URL")
    return _UK_PAYLOAD


def _transport_boom(url, headers, timeout):
    raise TimeoutError("simulated network timeout")


def test_parse_reported():
    block = grid._parse_uk_carbon_intensity(_UK_PAYLOAD)
    _check(block["carbon_intensity_gco2_per_kwh"] == 173.0,
           "carbon intensity carried VERBATIM (actual preferred over forecast)")
    _check(block["carbon_intensity_label"] == grid.GRID_LABEL_REPORTED,
           "value present -> REPORTED label")
    _check(block["carbon_intensity_kind"] == grid.CI_KIND_GRID_AVERAGE,
           "kind is grid_average (never over-claimed as marginal)")
    _check(block["carbon_intensity_index"] == "moderate", "index carried verbatim")
    _check(block["observed_at"] == "2026-07-09T18:30Z", "observed_at from feed")
    _check(block["region"] == "GB", "region GB")
    _check(block["source"] == grid.UK_CI_NATIONAL_URL, "source URL recorded")
    _check(block["price_per_mwh"] is None
           and block["price_label"] == grid.GRID_LABEL_UNAVAILABLE,
           "UK CI publishes no price -> honest UNAVAILABLE (never invented)")


def test_fetch_ok_and_unavailable():
    ok = grid.fetch_grid_context(_transport=_transport_ok)
    _check(ok["carbon_intensity_gco2_per_kwh"] == 173.0, "fetch (mock) -> REPORTED value")

    boom = grid.fetch_grid_context(_transport=_transport_boom)
    _check(boom["carbon_intensity_gco2_per_kwh"] is None
           and boom["carbon_intensity_label"] == grid.GRID_LABEL_UNAVAILABLE,
           "network failure -> honest UNAVAILABLE (never a fabricated number)")
    _check("TimeoutError" in boom["note"], "UNAVAILABLE note names the failure kind")

    bad = grid.fetch_grid_context(_transport=lambda u, h, t: {"data": []})
    _check(bad["carbon_intensity_label"] == grid.GRID_LABEL_UNAVAILABLE,
           "malformed shape -> honest UNAVAILABLE")

    unk = grid.fetch_grid_context(provider="watttime", _transport=_transport_ok)
    _check(unk["carbon_intensity_label"] == grid.GRID_LABEL_UNAVAILABLE,
           "keyless-only: unknown provider -> UNAVAILABLE, no raise")


def test_sanitize_coerces():
    dirty = {
        "provider": "uk_carbon_intensity",
        "carbon_intensity_gco2_per_kwh": float("nan"),  # must become null+UNAVAILABLE
        "price_per_mwh": "not-a-number",
    }
    clean = grid.sanitize_grid_context(dirty)
    _check(clean["carbon_intensity_gco2_per_kwh"] is None
           and clean["carbon_intensity_label"] == grid.GRID_LABEL_UNAVAILABLE,
           "NaN intensity -> null + UNAVAILABLE (never leaks a non-finite number)")
    _check(clean["price_per_mwh"] is None
           and clean["price_label"] == grid.GRID_LABEL_UNAVAILABLE,
           "non-numeric price -> null + UNAVAILABLE")
    _check(grid.sanitize_grid_context(None) is None, "None -> None (legacy default)")
    # A real value must be labelled REPORTED.
    good = grid.sanitize_grid_context({"carbon_intensity_gco2_per_kwh": 210})
    _check(good["carbon_intensity_label"] == grid.GRID_LABEL_REPORTED,
           "finite value -> REPORTED label")


def test_unavailable_block_all_null():
    u = grid.unavailable_grid_context("uk_carbon_intensity", grid.UK_CI_NATIONAL_URL,
                                      region="GB", reason="offline")
    for k in ("carbon_intensity_gco2_per_kwh", "carbon_intensity_index",
              "price_per_mwh", "observed_at"):
        _check(u[k] is None, f"UNAVAILABLE block: {k} is null")
    _check(u["carbon_intensity_label"] == grid.GRID_LABEL_UNAVAILABLE
           and u["price_label"] == grid.GRID_LABEL_UNAVAILABLE,
           "UNAVAILABLE block: both labels UNAVAILABLE")
    _check(u["fetched_at"], "UNAVAILABLE block records fetched_at (when we tried)")


def _legacy_kwargs():
    return dict(
        provenance={"served_by": "box_gpu", "sovereign": True, "tier": "sovereign",
                    "energy_source": "grid", "attempts": []},
        model="szl-fast",
        usage={"total_tokens": 5},
        req_digest="deadbeef",
    )


def test_build_body_backcompat_and_grid():
    legacy = R.build_body(**_legacy_kwargs())
    _check("grid_context" not in legacy, "grid_context ABSENT by default")
    _check(legacy["energy"] == "UNAVAILABLE",
           "energy stays UNAVAILABLE (grid_context never measures a joule)")

    block = grid.fetch_grid_context(_transport=_transport_ok)
    withgc = R.build_body(grid_context=block, **_legacy_kwargs())
    _check(withgc["grid_context"]["carbon_intensity_gco2_per_kwh"] == 173.0,
           "grid_context carried into receipt body when present")
    _check(withgc["energy"] == "UNAVAILABLE",
           "energy STILL UNAVAILABLE with grid_context attached (no over-claim)")

    # Byte-identical back-compat: dropping grid_context reproduces the legacy body.
    withgc.pop("grid_context")
    _check(json.dumps(withgc, sort_keys=True) == json.dumps(legacy, sort_keys=True),
           "body minus grid_context is byte-identical to legacy body")


def test_current_grid_context_nonblocking_and_warms():
    grid._reset_cache_for_tests()

    # _sync path (tests only) deterministically populates + returns the cache.
    b = grid.current_grid_context(_transport=_transport_ok, _sync=True)
    _check(b["carbon_intensity_gco2_per_kwh"] == 173.0, "sync path warms cache -> REPORTED")
    b2 = grid.current_grid_context(_transport=_transport_boom)  # fresh cache -> no fetch
    _check(b2 is b, "fresh cache returned without a new fetch (transport not called)")

    # Non-blocking guarantee: a SLOW transport must not block the request path.
    grid._reset_cache_for_tests()

    def _slow(url, headers, timeout):
        time.sleep(0.5)
        return _UK_PAYLOAD

    t0 = time.time()
    first = grid.current_grid_context(_transport=_slow)  # cold cache
    elapsed = time.time() - t0
    _check(elapsed < 0.2, f"request path is non-blocking (returned in {elapsed:.3f}s)")
    _check(first is None, "cold cache returns None (receipt byte-identical until warm)")

    # Background refresh eventually warms the cache (poll up to ~2s).
    warmed = None
    for _ in range(40):
        time.sleep(0.1)
        warmed, fresh = grid._cached(grid._DEFAULT_TTL)
        if warmed is not None:
            break
    _check(warmed is not None
           and warmed["carbon_intensity_gco2_per_kwh"] == 173.0,
           "background daemon refresh warmed the cache with REPORTED value")


if __name__ == "__main__":
    test_parse_reported()
    test_fetch_ok_and_unavailable()
    test_sanitize_coerces()
    test_unavailable_block_all_null()
    test_build_body_backcompat_and_grid()
    test_current_grid_context_nonblocking_and_warms()
    if FAILED:
        print(f"RESULT: {FAILED} check(s) FAILED")
        sys.exit(1)
    print("RESULT: grid_context is honest, non-blocking, and back-compatible")
