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

Env knobs (all optional):
  SZL_SPEND_CAP_USD      hard cumulative cap in USD           (default 25)
  SZL_SPEND_KILL_FILE    presence = emergency stop            (/opt/alloyscape/.spend-KILL)
  SZL_SPEND_LEDGER_FILE  append-only ledger path              (/opt/alloyscape/.szl-router-spend.jsonl)
  SZL_PAID_USD_PER_1K    per-1k-token estimate for paid tier  (default 0.003)
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


def estimate_usd(result: Optional[Dict[str, Any]]) -> float:
    """Conservative, honest USD estimate for one paid call from its usage block."""
    usage = (result or {}).get("usage") or {}
    tot = usage.get("total_tokens")
    if tot is None:
        tot = (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0) or None
    rate = _envf("SZL_PAID_USD_PER_1K", 0.003)
    if tot:
        return round((float(tot) / 1000.0) * rate, 6)
    return _envf("SZL_PAID_CALL_USD", 0.01)


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
