# SPDX-License-Identifier: Apache-2.0
"""szl_router.grid — honest, best-effort, cached ``grid_context`` for receipts.

WHAT THIS IS (and is NOT)
-------------------------
A tiny helper that lets a routed answer's ``x-szl-receipt`` OPTIONALLY document
the *observed grid signal* at route time — the grid's carbon intensity
(gCO2/kWh) — as a ``grid_context`` block. It mirrors, field-for-field, the
honest pass-through pattern shipped in ``szl-energy-attest`` (PR #17,
``szl_energy_attest/_grid.py``) so a receipt from either organ carries the same
shape. It lets a run DOCUMENT that it happened in a cleaner / dirtier window.

It does **NOT** measure joules and it does **NOT** create energy. It records a
third-party REPORTED signal, verbatim, with its source URL + timestamps. The
router runs no joule meter, so the receipt's ``energy`` field stays
``"UNAVAILABLE"`` exactly as before — a ``grid_context`` block NEVER turns an
unmeasured run into a measured one.

DOCTRINE (never weakened here)
------------------------------
  * Every ``grid_context`` numeric field is a REPORTED pass-through from a real
    public signal, carried verbatim with ``source`` + ``observed_at`` +
    ``fetched_at``. NEVER invented, modelled, or defaulted.
  * A missing / unreachable / malformed signal yields an honest ``null`` value
    with label ``UNAVAILABLE`` — never a fabricated number.
  * The **UK Carbon Intensity API** is keyless and the only provider here. It
    reports *grid-average* carbon intensity (actual/forecast), NOT marginal — we
    label ``carbon_intensity_kind`` honestly and never upgrade it to "marginal".
    It publishes no price, so ``price_per_mwh`` is ``null`` / ``UNAVAILABLE``.
  * OPT-IN + NON-BLOCKING: the block is attached only when
    ``SZL_RECEIPT_GRID_CONTEXT`` is truthy, and the network fetch NEVER runs on
    the request path — a background daemon thread refreshes a short-TTL cache
    and the request only ever reads the cache (so routing latency is untouched
    and a receipt is byte-identical when the feature is off).

Pure stdlib (urllib + json + threading). No third-party dependencies. Network
access is injected via ``_transport`` so tests are fully hermetic (no live HTTP).
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
import urllib.request
from typing import Any, Callable, Dict, Optional

# Honest per-field labels for grid_context values (identical to szl-energy-attest).
GRID_LABEL_REPORTED = "REPORTED"        # verbatim from a real external signal
GRID_LABEL_UNAVAILABLE = "UNAVAILABLE"  # signal missing/unreachable -> value is null

# The single keyless provider wired here.
PROVIDER_UK_CARBON_INTENSITY = "uk_carbon_intensity"
UK_CI_NATIONAL_URL = "https://api.carbonintensity.org.uk/intensity"

# Kind of carbon-intensity number, so we never over-claim "marginal".
CI_KIND_GRID_AVERAGE = "grid_average"   # UK CI API (actual/forecast average mix)

# Short server-side cache: don't hammer the public feed and keep the request
# path reading a warm value. Overridable via env for ops tuning.
_DEFAULT_TTL = float(os.environ.get("SZL_RECEIPT_GRID_TTL", "300") or "300")

# Transport callable signature: (url, headers, timeout) -> parsed JSON (dict).
Transport = Callable[[str, Dict[str, str], float], Any]

_ENV_ENABLE = "SZL_RECEIPT_GRID_CONTEXT"

# Module cache + refresh guards.
_CACHE: Dict[str, Any] = {"ts": 0.0, "block": None}
_CACHE_LOCK = threading.Lock()
_REFRESH_LOCK = threading.Lock()
_REFRESH_INFLIGHT = False


def _env_truthy(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def enabled() -> bool:
    """True iff receipts should carry a ``grid_context`` block (OPT-IN, default OFF).

    Kept default-off so existing receipts stay byte-identical unless an operator
    explicitly sets ``SZL_RECEIPT_GRID_CONTEXT=1``.
    """
    return _env_truthy(_ENV_ENABLE, default=False)


def _finite(x: Any) -> Optional[float]:
    """Coerce to a finite float or None (bool / NaN / inf / junk -> None).

    A non-finite or non-numeric value NEVER reaches a receipt as a real number.
    """
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    xf = float(x)
    return xf if math.isfinite(xf) else None


def _iso_utc(epoch: Optional[float] = None) -> str:
    """ISO-8601 UTC timestamp (e.g. ``2026-07-09T19:05:00Z``)."""
    t = time.gmtime(epoch if epoch is not None else time.time())
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", t)


def _urllib_transport(url: str, headers: Dict[str, str], timeout: float) -> Any:
    """Default real HTTP GET returning parsed JSON. Stdlib only.

    Tests inject a fake ``_transport`` instead of touching the network, so this
    is never exercised in CI.
    """
    req = urllib.request.Request(url, headers=dict(headers or {}))
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def unavailable_grid_context(provider: str,
                             source: str,
                             *,
                             region: Optional[str] = None,
                             reason: str = "") -> Dict[str, Any]:
    """An honest all-``null`` grid_context: signal missing/unreachable/absent.

    Every value field is ``null`` and labelled ``UNAVAILABLE``. NOTHING is
    invented. ``fetched_at`` records when we tried.
    """
    note = ("REPORTED grid signal UNAVAILABLE — no value fetched; nothing "
            "invented. This does not create or measure energy.")
    if reason:
        note = "%s (%s)" % (note, reason)
    return {
        "provider": str(provider),
        "source": str(source),
        "region": (str(region) if region is not None else None),
        "observed_at": None,
        "fetched_at": _iso_utc(),
        "carbon_intensity_gco2_per_kwh": None,
        "carbon_intensity_kind": None,
        "carbon_intensity_index": None,
        "carbon_intensity_label": GRID_LABEL_UNAVAILABLE,
        "price_per_mwh": None,
        "price_label": GRID_LABEL_UNAVAILABLE,
        "note": note,
    }


def sanitize_grid_context(block: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Normalise a grid_context into the honest canonical shape.

    Coerces numeric fields through ``_finite`` (non-finite / non-numeric -> None
    + ``UNAVAILABLE``), forces honest labels to follow the actual value (a null
    value can never be labelled ``REPORTED``), and drops unknown keys. Returns
    ``None`` for ``None`` input. Byte-identical field set to szl-energy-attest.
    """
    if block is None:
        return None
    if not isinstance(block, dict):
        raise TypeError("grid_context must be a dict or None")

    ci = _finite(block.get("carbon_intensity_gco2_per_kwh"))
    ci_label = (GRID_LABEL_REPORTED if ci is not None else GRID_LABEL_UNAVAILABLE)
    price = _finite(block.get("price_per_mwh"))
    price_label = (GRID_LABEL_REPORTED if price is not None else GRID_LABEL_UNAVAILABLE)

    def _s(key: str) -> Optional[str]:
        v = block.get(key)
        return str(v) if v is not None else None

    return {
        "provider": str(block.get("provider", "unspecified")),
        "source": _s("source") or "UNAVAILABLE",
        "region": _s("region"),
        "observed_at": _s("observed_at"),
        "fetched_at": _s("fetched_at") or _iso_utc(),
        "carbon_intensity_gco2_per_kwh": (None if ci is None else round(ci, 6)),
        "carbon_intensity_kind": _s("carbon_intensity_kind"),
        "carbon_intensity_index": _s("carbon_intensity_index"),
        "carbon_intensity_label": ci_label,
        "price_per_mwh": (None if price is None else round(price, 6)),
        "price_label": price_label,
        "note": str(block.get(
            "note",
            "REPORTED pass-through grid signal; not a MEASURED joule. This does "
            "not create or measure energy.")),
    }


def _parse_uk_carbon_intensity(payload: Any) -> Dict[str, Any]:
    """Map the UK Carbon Intensity API JSON to a grid_context block.

    Shape (national): ``{"data":[{"from","to","intensity":{"forecast","actual",
    "index"}}]}``. We prefer ``actual`` over ``forecast`` and carry it VERBATIM.
    A missing intensity yields an honest UNAVAILABLE block (never invented).
    UK CI reports grid-AVERAGE intensity and NO price -> price is UNAVAILABLE.
    """
    src = UK_CI_NATIONAL_URL
    try:
        rec = payload["data"][0]
        intensity = rec.get("intensity", {}) or {}
        actual = _finite(intensity.get("actual"))
        forecast = _finite(intensity.get("forecast"))
        value = actual if actual is not None else forecast
        if value is None:
            return unavailable_grid_context(
                PROVIDER_UK_CARBON_INTENSITY, src, region="GB",
                reason="no actual/forecast intensity in response")
        observed_at = rec.get("from")
        index = intensity.get("index")
        which = "actual" if actual is not None else "forecast"
        return sanitize_grid_context({
            "provider": PROVIDER_UK_CARBON_INTENSITY,
            "source": src,
            "region": "GB",
            "observed_at": observed_at,
            "fetched_at": _iso_utc(),
            "carbon_intensity_gco2_per_kwh": value,
            "carbon_intensity_kind": CI_KIND_GRID_AVERAGE,
            "carbon_intensity_index": index,
            "price_per_mwh": None,   # UK CI API publishes no price -> honest null
            "note": ("REPORTED grid-average carbon intensity (%s) from the UK "
                     "Carbon Intensity API, carried verbatim; NOT marginal, NOT "
                     "a MEASURED joule. This does not create or measure energy."
                     % which),
        })
    except (KeyError, IndexError, TypeError):
        return unavailable_grid_context(
            PROVIDER_UK_CARBON_INTENSITY, src, region="GB",
            reason="unexpected response shape")


def fetch_grid_context(provider: str = PROVIDER_UK_CARBON_INTENSITY,
                       *,
                       region: Optional[str] = None,
                       timeout: float = 5.0,
                       _transport: Optional[Transport] = None) -> Dict[str, Any]:
    """Fetch an honest ``grid_context`` block from the keyless UK grid signal.

    On ANY failure (network error, timeout, bad JSON, unknown provider) the
    return value is an honest UNAVAILABLE block — never a fabricated number and
    never a raised exception. ``_transport`` is an injectable
    ``(url, headers, timeout) -> json`` callable (defaults to the real stdlib
    HTTP GET) so tests never touch the network.

    This is the blocking primitive; the request path never calls it directly —
    ``current_grid_context`` runs it in a background thread (see below).
    """
    transport = _transport or _urllib_transport
    if provider != PROVIDER_UK_CARBON_INTENSITY:
        return unavailable_grid_context(
            str(provider), "UNAVAILABLE", region=region,
            reason="unknown/keyless-only provider")
    try:
        payload = transport(UK_CI_NATIONAL_URL,
                            {"Accept": "application/json"}, timeout)
    except Exception as e:  # noqa: BLE001 - network/parse failure is honest UNAVAILABLE
        return unavailable_grid_context(
            PROVIDER_UK_CARBON_INTENSITY, UK_CI_NATIONAL_URL, region="GB",
            reason="fetch failed: %s" % type(e).__name__)
    return _parse_uk_carbon_intensity(payload)


def _store(block: Optional[Dict[str, Any]]) -> None:
    with _CACHE_LOCK:
        _CACHE["block"] = block
        _CACHE["ts"] = time.time()


def _cached(max_age: float):
    now = time.time()
    with _CACHE_LOCK:
        block = _CACHE.get("block")
        fresh = block is not None and (now - float(_CACHE.get("ts", 0.0))) < max_age
    return block, fresh


def _spawn_refresh(provider: str, region: Optional[str], timeout: float,
                   transport: Optional[Transport]) -> None:
    """Kick off ONE background daemon refresh (deduped). Never blocks the caller."""
    global _REFRESH_INFLIGHT
    with _REFRESH_LOCK:
        if _REFRESH_INFLIGHT:
            return
        _REFRESH_INFLIGHT = True

    def _run() -> None:
        global _REFRESH_INFLIGHT
        try:
            block = fetch_grid_context(provider, region=region, timeout=timeout,
                                       _transport=transport)
            _store(block)  # REPORTED on success, honest UNAVAILABLE on failure
        except Exception:  # noqa: BLE001 - belt & suspenders; fetch already swallows
            pass
        finally:
            with _REFRESH_LOCK:
                _REFRESH_INFLIGHT = False

    threading.Thread(target=_run, name="szl-grid-refresh", daemon=True).start()


def current_grid_context(*,
                         provider: str = PROVIDER_UK_CARBON_INTENSITY,
                         region: Optional[str] = None,
                         max_age: float = _DEFAULT_TTL,
                         timeout: float = 5.0,
                         _transport: Optional[Transport] = None,
                         _sync: bool = False) -> Optional[Dict[str, Any]]:
    """Return the current cached ``grid_context`` WITHOUT blocking the caller.

    Contract (request-path safe):
      * If the cache is fresh -> return it immediately.
      * Else -> kick off a background daemon refresh and return whatever we have
        right now: a stale-but-honest block (still REPORTED with its own
        ``observed_at``/``fetched_at``) or ``None`` on the very first request
        (nothing attached, so the receipt stays byte-identical until warm).
    Never raises. ``_sync=True`` fetches inline (tests only) so the cache logic
    can be exercised deterministically without real HTTP or thread timing.
    """
    block, fresh = _cached(max_age)
    if fresh:
        return block
    if _sync:
        block = fetch_grid_context(provider, region=region, timeout=timeout,
                                   _transport=_transport)
        _store(block)
        return block
    _spawn_refresh(provider, region, timeout, _transport)
    return block


def _reset_cache_for_tests() -> None:
    """Clear the module cache (tests only)."""
    global _REFRESH_INFLIGHT
    with _CACHE_LOCK:
        _CACHE["block"] = None
        _CACHE["ts"] = 0.0
    with _REFRESH_LOCK:
        _REFRESH_INFLIGHT = False
