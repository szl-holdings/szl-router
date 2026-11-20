"""Direct provider-model statistics from recorded routing attempts.

2026-11-20 three-brain study consensus: DIRECT STATISTICS FIRST. Any
fixed-point standing (see trust_rank.py) must beat this scorecard in a
pre-registered inert comparison before it influences anything at all; until
then, this module is the honest telemetry surface.

Disclosure-only: this module computes, it never qualifies, reorders, or arms.
Every number here is an ESTIMATE from recorded evidence, with uncertainty
stated fail-closed (Wilson 95% lower bound on the success share). It is NOT
sovereign (it claims no hardware), spends nothing, and holds no secrets:
attempts arrive as plain data, keys are never read. Pure stdlib; no network.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

# An attempt record: (provider, model, ok, latency_s, ts_epoch_s).
Attempt = Tuple[str, str, bool, float, float]

_CARD_KEYS = frozenset({
    "provider", "model", "samples", "successes", "failures",
    "success_rate", "success_rate_wilson95_lower", "latency_mean_s",
    "latency_p50_s", "latency_p95_s", "actionable", "note",
})


def wilson_lower(successes: int, samples: int, z: float = 1.96) -> float:
    """Fail-closed Wilson score lower bound on a success share.

    With no samples the honest lower bound is 0.0: absence of evidence is
    not a good score, it is no score at all.
    """
    if samples <= 0 or successes < 0 or successes > samples:
        return 0.0
    n = float(samples)
    phat = successes / n
    denom = 1.0 + z * z / n
    centre = phat + z * z / (2.0 * n)
    margin = z * math.sqrt(phat * (1.0 - phat) / n + z * z / (4.0 * n * n))
    return max(0.0, min(1.0, (centre - margin) / denom))


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    """Deterministic, interpolation-free conservative quantile (ceil index)."""
    if not sorted_values:
        return 0.0
    idx = max(0, min(len(sorted_values) - 1, int(math.ceil(q * len(sorted_values))) - 1))
    return sorted_values[idx]


def scorecard(attempts: Sequence[Attempt], *, window_s: Optional[float] = None,
              min_samples: int = 30) -> Dict[str, Any]:
    """Per-(provider, model) statistics over recorded attempts.

    window_s: if given, only attempts with the LATEST ts in the data within
        that many seconds are counted (stale evidence is stale standing).
    min_samples: a card is "actionable" (worth reading) only at or above this
        sample count; below it the card is disclosed as insufficient evidence.
        This flag is INFORMATION ABOUT THE CARD, never a qualification.

    Output keys are pinned by test: this structure must never grow a
    "qualified" or "armed" field. Adding one is a doctrine violation.
    """
    rows: List[Attempt] = list(attempts)
    if window_s is not None and window_s > 0 and rows:
        latest = max(r[4] for r in rows)
        rows = [r for r in rows if latest - r[4] <= window_s]

    grouped: Dict[Tuple[str, str], List[Attempt]] = {}
    for r in rows:
        grouped.setdefault((r[0], r[1]), []).append(r)

    cards = {}
    for (provider, model), group in sorted(grouped.items()):
        samples = len(group)
        successes = sum(1 for r in group if r[2])
        latencies = sorted(float(r[3]) for r in group if r[3] is not None)
        card = {
            "provider": provider,
            "model": model,
            "samples": samples,
            "successes": successes,
            "failures": samples - successes,
            "success_rate": successes / samples if samples else 0.0,
            "success_rate_wilson95_lower": wilson_lower(successes, samples),
            "latency_mean_s": (sum(latencies) / len(latencies)) if latencies else 0.0,
            "latency_p50_s": _quantile(latencies, 0.50),
            "latency_p95_s": _quantile(latencies, 0.95),
            "actionable": samples >= min_samples,
            "note": "estimates from recorded attempts only; not qualification",
        }
        if set(card) != _CARD_KEYS:  # pragma: no cover - structural guard
            raise RuntimeError("scorecard card keys drifted")
        cards["%s:%s" % (provider, model)] = card

    return {
        "schema": "szl.router.scorecard/v1",
        "window_s": window_s,
        "min_samples": min_samples,
        "cards": cards,
        "note": "direct statistics; disclosure-only; consumes nothing, ranks "
                "nothing, qualifies nothing",
    }