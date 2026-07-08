"""szl-router local spend guard — SZL Sovereign Ops.

Enforces the org's hard USD spend cap + emergency kill-switch at the ONE place
money can actually leave: the paid-tier upstream call in core.chat(). Free and
sovereign tiers cost nothing and are never gated.

Design (matches the a11oy szl_spend_cap doctrine):
  * Pure stdlib, zero deps, runs anywhere.
  * Append-only, hash-linked JSON-lines ledger (tamper-evident: each row's
    digest chains the previous one, exactly like a receipt chain).
  * Shares the org kill-file convention (SZL_SPEND_KILL_FILE); a single
    `touch` of that file halts ALL paid spend instantly, everywhere.
  * Honest by construction: an estimate is labelled estimated; nothing is
    fabricated; a missing usage block falls back to a flat conservative figure.
  * HONEST PER-MODEL PRICING: paid frontier models are priced with their real
    asymmetric input/output list rates (not one flat number), so the ledger
    never systematically under-records frontier spend. Rates are conservative
    (rounded UP where uncertain) — an estimate may be conservative-high, never
    optimistic-low. Every row records the rate basis + token counts so the
    figure is auditable and correctable.

Env knobs (all optional):
  SZL_SPEND_CAP_USD      hard cumulative cap in USD           (default 25)
  SZL_SPEND_KILL_FILE    presence = emergency stop            (/opt/alloyscape/.spend-KILL)
  SZL_SPEND_LEDGER_FILE  append-only ledger path              (/opt/alloyscape/.szl-router-spend.jsonl)
  SZL_PAID_USD_PER_1K    flat per-1k fallback for UNKNOWN paid models (default 0.003)
  SZL_PAID_CALL_USD      flat per-call estimate if no usage   (default 0.01)
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

_GENESIS = "0" * 64
_LOCK = threading.Lock()

# --- Per-model paid pricing (USD per 1K tokens), input/output split ----------
# Public list prices, matched case-insensitively by SUBSTRING against the served
# upstream model id, most-specific first. Rounded UP where uncertain so the
# ledger never UNDER-records (honesty doctrine + fail-closed on cost). These are
# estimates and are labelled as such in every ledger row; correct them here as
# published prices change.
_MODEL_RATES: List[Tuple[str, float, float]] = [
    # (substring, usd_per_1k_input, usd_per_1k_output)
    # -- Moonshot / Kimi (the armed paid-grid frontier route) --
    ("kimi-k2",            0.00060, 0.00250),  # kimi-k2-*-preview (cache-miss input)
    ("moonshot-v1-128k",   0.00060, 0.00250),
    ("moonshot-v1-32k",    0.00030, 0.00120),
    ("moonshot-v1-8k",     0.00020, 0.00060),
    ("kimi",               0.00060, 0.00250),
    ("moonshot",           0.00060, 0.00250),
    # -- OpenAI (only if later armed) --
    ("gpt-4o-mini",        0.00015, 0.00060),
    ("gpt-4o",             0.00250, 0.01000),
    ("gpt-4.1-mini",       0.00040, 0.00160),
    ("gpt-4.1-nano",       0.00010, 0.00040),
    ("gpt-4.1",            0.00200, 0.00800),
    ("o3-mini",            0.00110, 0.00440),
    ("o1-mini",            0.00110, 0.00440),
    # -- Anthropic (only if later armed) --
    ("claude-3-5-haiku",   0.00080, 0.00400),
    ("claude-3-5-sonnet",  0.00300, 0.01500),
    ("claude-3-7-sonnet",  0.00300, 0.01500),
    ("claude-sonnet",      0.00300, 0.01500),
    ("claude-opus",        0.01500, 0.07500),
    ("claude",             0.00300, 0.01500),
    # -- DeepSeek / others --
    ("deepseek-r1",        0.00055, 0.00219),
    ("deepseek",           0.00027, 0.00110),
    ("glm-4",              0.00050, 0.00050),
]


def _envf(name: str, default: float) -> float:
    try:
        v = os.environ.get(name, "").strip()
        return float(v) if v else float(default)
    except Exception:
        return float(default)


def cap_usd() -> float:
    return _envf("SZL_SPEND_CAP_USD", 25.0)


def kill_file() -> str:
    return os.environ.get("SZL_SPEND_KILL_FILE", "/opt/alloyscape/.spend-KILL")


def ledger_file() -> str:
    return os.environ.get("SZL_SPEND_LEDGER_FILE", "/opt/alloyscape/.szl-router-spend.jsonl")


def kill_engaged() -> bool:
    return os.path.exists(kill_file())


def _read_entries() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        with open(ledger_file(), "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    # A corrupt row is surfaced by the chain check, never silently repaired.
                    out.append({"amount_usd": 0, "digest": "CORRUPT", "corrupt": True})
    except FileNotFoundError:
        pass
    return out


def _digest(prev: str, body: Dict[str, Any]) -> str:
    h = hashlib.sha256()
    h.update(prev.encode("utf-8"))
    h.update(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return h.hexdigest()


def _chain_intact(entries: List[Dict[str, Any]]) -> bool:
    prev = _GENESIS
    for e in entries:
        if e.get("corrupt"):
            return False
        body = {k: v for k, v in e.items() if k != "digest"}
        if _digest(prev, body) != e.get("digest"):
            return False
        prev = e.get("digest") or ""
    return True


def spent_usd() -> float:
    return round(sum(float(e.get("amount_usd", 0) or 0) for e in _read_entries()), 6)


def rate_for(model: Optional[str]) -> Tuple[float, float, str]:
    """Return (usd_per_1k_input, usd_per_1k_output, basis) for a served model.

    Falls back to a flat symmetric per-1k rate for any model not in the table so
    an unknown paid model is still recorded (conservatively), never dropped."""
    m = (model or "").lower().strip()
    if m:
        for sub, ri, ro in _MODEL_RATES:
            if sub in m:
                return ri, ro, "table:" + sub
    flat = _envf("SZL_PAID_USD_PER_1K", 0.003)
    return flat, flat, "flat-fallback"


def estimate_detail(result: Optional[Dict[str, Any]], model: Optional[str] = None) -> Dict[str, Any]:
    """Honest, auditable USD estimate for one paid call.

    Prices prompt/completion tokens with the model's asymmetric input/output
    rates. If only a total is given, prices the whole total at the higher
    (output) rate — conservative-high so we never under-record. If there is no
    usage block at all, falls back to a flat per-call figure. Returns the amount
    plus the rate basis + token counts for the ledger."""
    usage = (result or {}).get("usage") or {}
    ri, ro, basis = rate_for(model)
    pt = usage.get("prompt_tokens")
    ct = usage.get("completion_tokens")
    tot = usage.get("total_tokens")
    if pt is not None or ct is not None:
        pt_i = int(pt or 0)
        ct_i = int(ct or 0)
        amt = (pt_i / 1000.0) * ri + (ct_i / 1000.0) * ro
        return {"amount_usd": round(amt, 6), "estimated": True, "basis": basis,
                "model": model, "rate_in_per_1k": ri, "rate_out_per_1k": ro,
                "prompt_tokens": pt_i, "completion_tokens": ct_i}
    if tot is not None:
        tot_i = int(tot or 0)
        amt = (tot_i / 1000.0) * max(ri, ro)
        return {"amount_usd": round(amt, 6), "estimated": True,
                "basis": basis + "+total-as-output", "model": model,
                "rate_in_per_1k": ri, "rate_out_per_1k": ro, "total_tokens": tot_i}
    return {"amount_usd": _envf("SZL_PAID_CALL_USD", 0.01), "estimated": True,
            "basis": "flat-per-call-no-usage", "model": model}


def estimate_usd(result: Optional[Dict[str, Any]], model: Optional[str] = None) -> float:
    """Back-compatible thin wrapper — returns just the estimated USD amount."""
    return estimate_detail(result, model)["amount_usd"]


def allow(estimated_usd: float = 0.0) -> Tuple[bool, str]:
    """Advisory gate for a PAID call. Returns (allowed, reason)."""
    if kill_engaged():
        return False, "kill-switch engaged (%s)" % kill_file()
    cap = cap_usd()
    spent = spent_usd()
    if spent >= cap:
        return False, "cap reached ($%.4f / $%.2f)" % (spent, cap)
    if estimated_usd and (spent + float(estimated_usd)) > cap:
        return False, "would exceed cap ($%.4f + $%.4f > $%.2f)" % (spent, estimated_usd, cap)
    return True, "ok"


def record(amount_usd: float, source: str = "", meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Append one estimated-USD row to the hash-linked ledger."""
    amt = round(float(amount_usd or 0), 6)
    with _LOCK:
        entries = _read_entries()
        prev = entries[-1].get("digest") if entries and entries[-1].get("digest") else _GENESIS
        if prev == "CORRUPT" or not prev:
            prev = _GENESIS
        body: Dict[str, Any] = {"ts": round(time.time(), 3), "amount_usd": amt,
                                "source": source, "estimated": True}
        if meta:
            body["meta"] = meta
        rec = dict(body)
        rec["digest"] = _digest(prev, body)
        path = ledger_file()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
    return rec


def state() -> Dict[str, Any]:
    """Honest snapshot for the /v1/spend readout (mirrors a11oy /spend/state)."""
    entries = _read_entries()
    cap = cap_usd()
    spent = round(sum(float(e.get("amount_usd", 0) or 0) for e in entries), 6)
    return {
        "schema": "szl.router_spend/v1",
        "cap_usd": cap,
        "spent_usd": spent,
        "remaining_usd": round(cap - spent, 6),
        "pct_used": round(100.0 * spent / cap, 2) if cap > 0 else None,
        "armed": True,
        "tripped": spent >= cap,
        "kill_file_engaged": kill_engaged(),
        "kill_file": kill_file(),
        "entries": len(entries),
        "chain": {"intact": _chain_intact(entries), "entries": len(entries)},
        "tail": entries[-8:],
        "ledger_file": ledger_file(),
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
