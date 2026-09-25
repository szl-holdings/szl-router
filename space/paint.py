"""Fail-closed organ paint bind.

The HUD is a function of the gate. Missing cycle JSON is UNAVAILABLE, not green.
HARD_DENY / LAMBDA_VETO / DENY_DEFAULT / ESCALATE cannot paint ALLOW.
Lambda stays Conjecture 1. productionPromotion cannot authorize chrome.
Arithmetic-would-allow is a ghost and cannot enable a control.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable

CYCLE_SCHEMA = "szl.frontier.ouroboros-cycle.v1"
ORGAN_CYCLE_URL = (
    "https://szlholdings-szl-frontier.hf.space/frontier/ouroboros-cycle.v1.json"
)
CYCLE_PATH = "/frontier/ouroboros-cycle.v1.json"
ORGAN_CYCLE_TIMEOUT_SECONDS = 5
DENY_VERDICTS = frozenset(
    {"HARD_DENY", "LAMBDA_VETO", "DENY_DEFAULT", "ESCALATE"}
)

Paint = str
UrlOpen = Callable[..., Any]


def paint_from_cycle(cycle: dict[str, Any] | None) -> Paint:
    if not isinstance(cycle, dict):
        return "UNAVAILABLE"
    if cycle.get("schema") != CYCLE_SCHEMA:
        return "UNAVAILABLE"
    if cycle.get("productionPromotion") is True:
        return "DENY"
    if cycle.get("lambda") != "CONJECTURE_1" or cycle.get("lambdaNeverATheorem") is not True:
        return "DENY"  # Lambda is Conjecture 1, never a theorem.
    if cycle.get("authority") != "PROPOSAL_ONLY":
        return "DENY"
    if cycle.get("invariantsOk") is not True:
        return "DENY"
    shadow = cycle.get("shadow")
    if isinstance(shadow, dict) and shadow.get("executable") is True:
        return "DENY"
    if cycle.get("verdict") == "ALLOW":
        return "ALLOW"
    if cycle.get("verdict") in DENY_VERDICTS:
        return "DENY"
    return "UNAVAILABLE"


def allow_chrome(paint: Paint) -> bool:
    return paint == "ALLOW"


def paint_tone(paint: Paint) -> str:
    if paint == "ALLOW":
        return "allow"
    if paint == "DENY":
        return "deny"
    return "pending"


def paint_label(paint: Paint, verdict: str | None = None) -> str:
    if paint == "UNAVAILABLE":
        return "UNAVAILABLE"
    if paint == "DENY":
        return str(verdict or "DENY")
    return "ALLOW"


def load_published_cycle(
    *,
    url: str = ORGAN_CYCLE_URL,
    timeout: float = ORGAN_CYCLE_TIMEOUT_SECONDS,
    urlopen: UrlOpen | None = None,
) -> dict[str, Any] | None:
    opener = urlopen or urllib.request.urlopen
    try:
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"Accept": "application/json", "Cache-Control": "no-store"},
        )
        with opener(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            if status not in (None, 200):
                return None
            raw = response.read()
        body = json.loads(raw.decode("utf-8"))
    except (
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
        UnicodeError,
        urllib.error.URLError,
        TimeoutError,
    ):
        return None
    if not isinstance(body, dict):
        return None
    if body.get("schema") != CYCLE_SCHEMA:
        return None
    return body
