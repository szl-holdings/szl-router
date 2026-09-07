from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CONTRACT = ROOT / "frontier" / "k2-candidate-2026-09-07.json"


def _contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def test_k2_candidate_is_fail_closed() -> None:
    contract = _contract()
    assert contract["schema"] == "szl.frontier.router-candidate.v1"
    assert contract["default_effect"] == "DENY"
    assert contract["routing_eligibility"] is False
    assert contract["production_promotion"] is False
    assert contract["canonical_frontier_revision"] == "94a039d086d1343d1fcfe5bca617f601006ca05b"
    assert contract["serve_contract_revision"] == "74c2fa12ee3cd17cc8d80ac85801f11683912176"


def test_k2_source_identity_and_receipts_are_required() -> None:
    candidate = _contract()["candidate"]
    assert candidate["status"] == "CANDIDATE_DISABLED"
    assert candidate["route_class"] == "sovereign-evaluation-only"
    assert candidate["qualification_receipt_required"] is True
    assert candidate["production_authority"] is False
    assert candidate["source_revision"] == "05cab0a4d7150c1c460a000b37ff40cc1af2feaa"
    assert candidate["artifact_fingerprint"] == "259e31e5a7143d4f6cca70ed238296e9374b0a51aa497563f62e10af990ec9e2"
    receipts = " ".join(candidate["required_receipts"]).lower()
    assert "frontier" in receipts
    assert "nemo" in receipts
    assert "serve" in receipts
    assert "a11oy" in receipts
    assert candidate["fallback"]


def test_k2_candidate_cannot_silently_enter_production() -> None:
    candidate = _contract()["candidate"]
    prohibited = " ".join(candidate["prohibited"]).lower()
    assert "automatic enablement" in prohibited
    assert "production routing" in prohibited
    assert "silent model/revision/quantization/runtime substitution" in prohibited
    assert "bypassing a11oy" in prohibited
