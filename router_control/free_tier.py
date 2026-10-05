# SPDX-License-Identifier: Apache-2.0
"""Source-owned, read-only free-tier evaluation options, not a quota bypass.

python -m router_control.free_tier prints the same dossier used by the control UI.
This module has no network, environment-credential, file-write or registry access.
A documentation price is not account eligibility, remaining quota or permission
for disclosure. All returned registry proposals remain disabled, including after
expiry. Refreshing the endpoint never refreshes the underlying evidence date.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import re
from typing import Any

SCHEMA = "szl.router-free-provider-options/v1"
REVIEWED_ON = date(2026, 10, 4)
RECHECK_ON = date(2026, 10, 11)  # Local review deadline, NOT a provider offer guarantee.
MAX_DOSSIER_BYTES = 32_768
OWNER = "szl-holdings/szl-router"


@dataclass(frozen=True)
class Option:
    id: str
    provider_label: str
    alias: str
    model: str
    base_url: str
    token_env: str
    price_basis: str
    sources: tuple[str, ...]
    limitations: tuple[str, ...]
    # Reported public limits, never the user's account balance or a reservation.
    reported_quota: tuple[tuple[str, int], ...] = ()


OPTIONS = (
    Option(
        "zai-standard-flash47-review", "Z.ai standard API", "zai-flash47-review",
        "glm-4.7-flash", "https://api.z.ai/api/paas/v4", "ZAI_API_KEY",
        "SOURCE_DOCUMENTATION_LISTS_FREE_INPUT_AND_OUTPUT",
        ("https://docs.z.ai/guides/overview/pricing",
         "https://docs.z.ai/guides/overview/quick-start"),
        ("Standard API only; not the distinct Coding Plan or bigmodel.cn endpoint.",
         "FlashX, GLM-5.x and built-in web search are outside this candidate.",
         "No account-specific quota, eligibility, retention or tool-use contract qualified."),
    ),
    Option(
        "zai-standard-flash45-review", "Z.ai standard API", "zai-flash45-review",
        "glm-4.5-flash", "https://api.z.ai/api/paas/v4", "ZAI_API_KEY",
        "SOURCE_DOCUMENTATION_LISTS_FREE_INPUT_AND_OUTPUT",
        ("https://docs.z.ai/guides/overview/pricing",
         "https://docs.z.ai/guides/overview/quick-start"),
        ("Separate exact model candidate; no transfer of Flash47 qualification.",
         "Built-in tools, paid variants and endpoint substitutions are not authorized.",
         "No account-specific quota, eligibility, retention or tool-use contract qualified."),
    ),
    Option(
        "groq-oss120-free-review", "Groq Free Plan", "groq-oss120-review",
        "openai/gpt-oss-120b", "https://api.groq.com/openai/v1", "GROQ_API_KEY",
        "SOURCE_DOCUMENTATION_LISTS_FREE_PLAN_WITH_LIMITS",
        ("https://console.groq.com/docs/rate-limits",
         "https://console.groq.com/docs/openai",
         "https://console.groq.com/docs/your-data"),
        ("Exact account plan, exceptions, billing controls and remaining quota are unverified.",
         "Limits apply at organization level; do not rotate accounts or retry an exhausted quota.",
         "Data retention has documented exceptions; no private-source disclosure is granted."),
        (("requests_per_minute", 30), ("requests_per_day", 1000),
         ("tokens_per_minute", 8000), ("tokens_per_day", 200000)),
    ),
)

BLOCKERS = (
    "ACCOUNT_ELIGIBILITY_UNVERIFIED", "QUOTA_REMAINING_UNVERIFIED",
    "ZERO_BILLING_ENFORCEMENT_UNVERIFIED", "DISCLOSURE_AND_RETENTION_UNQUALIFIED",
    "CREDENTIAL_NOT_INSPECTED", "MODEL_ALIAS_MUTABLE", "NATIVE_COMPLETION_UNTESTED",
    "CALLER_TOOL_CONTRACT_UNQUALIFIED", "ACTIVATION_SOURCE_ADMISSION_REQUIRED",
)


def evidence_state(now: datetime) -> str:
    """Require an aware clock; source-date expiry is unaffected by request time."""
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("an aware observation clock is required")
    today = now.astimezone(timezone.utc).date()
    if today < REVIEWED_ON:
        return "CLOCK_BEFORE_REVIEW"
    return "RECHECK_REQUIRED" if today >= RECHECK_ON else "DATED_DOCUMENTATION_REVIEW"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def dossier(source_revision: str = "UNAVAILABLE", *, now: datetime | None = None) -> dict[str, Any]:
    state = evidence_state(now if now is not None else datetime.now(timezone.utc))
    revision = source_revision if isinstance(source_revision, str) and re.fullmatch(
        r"(?:[0-9a-f]{40}|[0-9a-f]{64})", source_revision
    ) else "UNAVAILABLE"
    blockers = list(BLOCKERS)
    if state != "DATED_DOCUMENTATION_REVIEW":
        blockers.append(state)
    rows = []
    for item in OPTIONS:
        rows.append({
            "id": item.id, "provider_label": item.provider_label,
            "upstream_model": item.model, "disposition": "HOLD",
            "basis": "REPORTED_NOT_SZL_MEASURED", "price_basis": item.price_basis,
            "primary_urls": list(item.sources), "limitations": list(item.limitations),
            "reported_quota": dict(item.reported_quota) or None,
            "remaining_quota": None, "actual_charge_usd": None,
            "upstream_source_revision": None,
            "documentation_identity": "MOVING_PAGE_NOT_IMMUTABLE_RELEASE",
            "account_checked": False, "inference_measured": False,
            "blockers": list(blockers),
            # Schema-compatible proposal DATA, never inserted into load_settings().
            "disabled_provider_proposal": {
                "id": item.id, "provider_type": "openai_https", "base_url": item.base_url,
                "models": {item.alias: item.model}, "token_env": item.token_env,
                "priority": 10000, "sovereignty": 0, "cost_tier": 0,
                "classifications": ["public"], "enabled": False,
            },
        })
    body = {
        "schema": SCHEMA, "owner": OWNER, "source_revision": revision,
        "scope": "READ_ONLY_EVALUATION_OPTIONS", "disposition": "HOLD",
        "reviewed_on": REVIEWED_ON.isoformat(), "recheck_on": RECHECK_ON.isoformat(),
        "evidence_state": state, "expiry_basis": "LOCAL_REVIEW_DEADLINE_NOT_OFFER_VALIDITY",
        "dispatch_authorized": False, "provider_calls_performed": False,
        "registry_mutated": False, "production_authorized": False,
        "paid_fallback_authorized": False, "private_data_disclosure_authorized": False,
        "codex_balance_changed": False,
        "proposal_cost_tier_basis": "ORDINAL_NOT_USD_CAP_OR_PRICE_QUALIFICATION",
        "first_test_requirement": "ONE_SYNTHETIC_REQUEST_AFTER_ACCOUNT_RIGHTS_AND_ZERO_BILLING_QUALIFICATION",
        "options": rows,
    }
    # Content-change key only: no generation time. A hash is not a signature/grant.
    body["semantic_progress_key"] = hashlib.sha256(_canonical(body)).hexdigest()
    if len(_canonical(body)) > MAX_DOSSIER_BYTES:
        raise ValueError("free-tier evaluation dossier exceeds its fixed byte bound")
    return body


def main() -> int:
    print(json.dumps(dossier(), indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0  # Successful report generation only; disposition remains HOLD.


if __name__ == "__main__":
    raise SystemExit(main())
