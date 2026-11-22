"""LLM spend governance with honest provider health (propose-only).

Doctrine (leader-study card r1, docs/leader-study-r1.md): enforce spend
caps BEFORE any provider call; never route when no policy exists (fail
closed, not default-open); provider health scores are decayed honest
EWMA over time-stamped samples - stale-only input yields UNKNOWN, never
green.

Pure Python, stdlib only. No provider (LiteLLM/Portkey/OpenRouter) is
contacted: policy checks, ledger arithmetic, and health scoring are
pure functions over injected data.

Honest limitation, verbatim: SpendLedger enforcement is local
bookkeeping only and does not constitute provider-side spend control;
HonestHealth UNKNOWN is never green and EWMA over stale-only input
yields UNKNOWN. Clock/timestamps are injected (now_ts / observed ts),
never ambient, for deterministic decay semantics. PROPOSAL_ONLY, unsigned; merges are
owned by the estate signature wall. No status claims are made about any
other pull request.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

UNKNOWN = "UNKNOWN"
HEALTHY = "HEALTHY"
DEGRADED = "DEGRADED"

HEALTHY_THRESHOLD = 0.8
SCORE_MIN, SCORE_MAX = 0.0, 1.0


class PolicyError(ValueError):
    """A spend policy or amount is malformed."""


@dataclass
class SpendRule:
    model: str
    monthly_usd_limit: float

    def __post_init__(self):
        if not self.model or not isinstance(self.model, str):
            raise PolicyError("model must be a non-empty string")
        if not self.monthly_usd_limit or self.monthly_usd_limit <= 0:
            raise PolicyError("monthly_usd_limit must be positive")


@dataclass
class EnforceDecision:
    model: str
    allowed: bool
    reason: str
    spent_usd: float
    limit_usd: float

    def as_dict(self) -> dict:
        return {"model": self.model, "allowed": self.allowed, "reason": self.reason,
                "spent_usd": self.spent_usd, "limit_usd": self.limit_usd}


class SpendLedger:
    """In-memory spend ledger; enforce() is the only admission path."""

    def __init__(self):
        self.rules: Dict[str, SpendRule] = {}
        self.spent: Dict[str, float] = {}

    def add_rule(self, rule: SpendRule):
        if not isinstance(rule, SpendRule):
            raise PolicyError("add_rule requires a SpendRule")
        self.rules[rule.model] = rule
        self.spent.setdefault(rule.model, 0.0)

    def enforce(self, model: str, usd: float) -> EnforceDecision:
        """Fail closed when no policy exists; reject before spend."""
        if usd is None or usd <= 0:
            raise PolicyError("usd must be positive")
        rule = self.rules.get(model)
        if rule is None:
            return EnforceDecision(model, False,
                                   "no spend policy for model: fail closed, no provider call",
                                   self.spent.get(model, 0.0), 0.0)
        spent = self.spent.get(model, 0.0)
        if spent + usd > rule.monthly_usd_limit:
            return EnforceDecision(model, False,
                                   "over budget: %.4f + %.4f > %.4f (cap enforced before any provider call)" %
                                   (spent, usd, rule.monthly_usd_limit),
                                   spent, rule.monthly_usd_limit)
        self.spent[model] = spent + usd
        return EnforceDecision(model, True, "within budget", self.spent[model], rule.monthly_usd_limit)


class HonestHealth:
    """Decayed EWMA over time-stamped samples; stale-only input is UNKNOWN.

    Honest limitation, disclosed: samples arrive by injection; this module
    cannot prove a sample's provenance - it scores what it is given and
    reports UNKNOWN when its inputs are stale or absent, never green.
    """

    def __init__(self, half_life_s: float = 3600.0, stale_after_s: Optional[float] = None):
        if half_life_s <= 0:
            raise PolicyError("half_life_s must be positive")
        self.half_life_s = half_life_s
        self.stale_after_s = stale_after_s if stale_after_s is not None else 3 * half_life_s
        self._samples: List[Tuple[float, float]] = []

    def observe(self, score: float, ts: float):
        if not (SCORE_MIN <= score <= SCORE_MAX):
            raise PolicyError("score must be within [0, 1]")
        self._samples.append((float(ts), float(score)))

    def score(self, now_ts: float) -> Tuple[str, Optional[float]]:
        """Return (state, value); UNKNOWN whenever the freshest input is stale."""
        if not self._samples:
            return (UNKNOWN, None)
        newest = max(ts for ts, _ in self._samples)
        if now_ts - newest > self.stale_after_s:
            return (UNKNOWN, None)  # stale-only input: never green
        weight_sum = 0.0
        weighted = 0.0
        for ts, s in self._samples:
            w = 0.5 ** ((now_ts - ts) / self.half_life_s)
            weight_sum += w
            weighted += w * s
        value = weighted / weight_sum if weight_sum > 0 else None
        if value is None:
            return (UNKNOWN, None)
        return (HEALTHY if value >= HEALTHY_THRESHOLD else DEGRADED, value)
