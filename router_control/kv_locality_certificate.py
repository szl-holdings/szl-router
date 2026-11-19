"""SZL evidence-discounted KV-locality interval certificate (offline only).

Original conservative interval arithmetic, conceptually informed by
vLLM/PagedAttention, SGLang/RadixAttention, NVIDIA Dynamo KV-aware routing
and llm-d. No copied third-party implementation or active routing integration.

Unverified cache events affect ONLY optimistic lower bounds. Worst-case upper
cost assumes zero cache reuse; an interval comparison does not authorize
inference, deployment, cost, quality, KV transfer, or source attestation.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any

SCHEMA = "szl.router.kv-locality-certificate/v1"
INPUT_SCHEMA = "szl.router.kv-locality-observation/v1"
DOMAIN = b"szl.router.kv-locality-certificate/v1\x00"
SOURCE_SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
BOOT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,95}\Z")
HOSTS = frozenset({"gate1", "cuda"})
ROOT_FIELDS = {"schema", "observed_at_utc", "protected_main_sha", "request", "workers"}
REQUEST_FIELDS = {
    "request_id", "source_sha", "model_sha256", "tokenizer_sha256",
    "prefix_sha256", "prompt_tokens", "output_tokens", "classification", "cache_layout",
}
WORKER_FIELDS = {
    "host", "observed_at_utc", "source_sha", "model_sha256",
    "tokenizer_sha256", "boot_id", "model_identity_verified",
    "serving_owner_exclusive", "canary_fresh", "rate_observed_at_utc",
    "sample_requests", "prefill_us_per_token", "decode_us_per_token",
    "queue_us_lower", "queue_us_upper", "cache_claim",
}
CLAIM_FIELDS = {
    "prefix_sha256", "source_sha", "model_sha256", "tokenizer_sha256",
    "boot_id", "observed_at_utc", "claimed_prefix_tokens", "receipt_sha256",
}
MAX_ESTIMATED_US = 10**15


class Hold(ValueError):
    """One fixed fail-closed reason; no upstream text or secrets."""


def require(ok: bool, code: str) -> None:
    if not ok:
        raise Hold(code)


def fields(obj: Any, keys: set[str], reason: str) -> dict:
    require(type(obj) is dict and set(obj) == keys, reason)
    return obj


def integer(value: Any, minimum: int, maximum: int, reason: str) -> int:
    require(type(value) is int and minimum <= value <= maximum, reason)
    return value


def digest(value: Any, pattern: re.Pattern[str], reason: str) -> str:
    require(type(value) is str and pattern.fullmatch(value) is not None, reason)
    return value


def age(value: Any, now: datetime, max_seconds: int) -> bool:
    """Invalid and naive timestamps fail closed; stale timestamps confer no credit."""
    require(type(value) is str and len(value) <= 40, "TIMESTAMP_INVALID")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise Hold("TIMESTAMP_INVALID") from None
    require(dt.tzinfo is not None and dt.utcoffset() is not None, "TIMESTAMP_NAIVE")
    seconds = (now - dt.astimezone(timezone.utc)).total_seconds()
    return -5 <= seconds <= max_seconds


def cache_opportunity(claim: Any, worker: dict, request: dict,
                      now: datetime) -> tuple[int, str]:
    """Untrusted, unsigned cache events are conditional scenarios, not credits."""
    if claim is None:
        return 0, "NO_CACHE_EVENT"
    fields(claim, CLAIM_FIELDS, "CACHE_EVENT_FIELDS")
    for name in ("prefix_sha256", "model_sha256", "tokenizer_sha256", "receipt_sha256"):
        digest(claim[name], DIGEST, "CACHE_EVENT_DIGEST")
    digest(claim["source_sha"], SOURCE_SHA, "CACHE_EVENT_SOURCE")
    digest(claim["boot_id"], BOOT, "CACHE_EVENT_BOOT")
    hit = integer(claim["claimed_prefix_tokens"], 0, request["prompt_tokens"],
                  "CACHE_PREFIX_TOKENS_INVALID")
    fresh = age(claim["observed_at_utc"], now, 30)
    matches = (
        claim["prefix_sha256"] == request["prefix_sha256"]
        and claim["source_sha"] == request["source_sha"]
        and claim["model_sha256"] == request["model_sha256"]
        and claim["tokenizer_sha256"] == request["tokenizer_sha256"]
        and claim["boot_id"] == worker["boot_id"]
    )
    if not fresh:
        return 0, "STALE_CACHE_CLAIM_IGNORED"
    if not matches:
        return 0, "CACHE_IDENTITY_MISMATCH_IGNORED"
    return hit, "UNSIGNED_HYPOTHETICAL_ONLY"


def inspect_worker(worker: dict, request: dict, now: datetime) -> dict:
    fields(worker, WORKER_FIELDS, "WORKER_FIELDS")
    require(worker["host"] in HOSTS, "UNEXPECTED_WORKER")
    digest(worker["source_sha"], SOURCE_SHA, "WORKER_SOURCE")
    for key in ("model_sha256", "tokenizer_sha256"):
        digest(worker[key], DIGEST, "WORKER_MODEL_IDENTITY")
    digest(worker["boot_id"], BOOT, "WORKER_BOOT_ID")
    for key in ("model_identity_verified", "serving_owner_exclusive", "canary_fresh"):
        require(type(worker[key]) is bool, "WORKER_TRUST_FIELD_INVALID")
    sample = integer(worker["sample_requests"], 0, 1000000, "SAMPLE_COUNT_INVALID")
    prefill = integer(worker["prefill_us_per_token"], 1, 1000000,
                      "PREFILL_RATE_INVALID")
    decode = integer(worker["decode_us_per_token"], 1, 1000000,
                     "DECODE_RATE_INVALID")
    qlow = integer(worker["queue_us_lower"], 0, 10**12, "QUEUE_LOWER_INVALID")
    qhigh = integer(worker["queue_us_upper"], 0, 10**12, "QUEUE_UPPER_INVALID")
    require(qlow <= qhigh, "QUEUE_BOUNDS_REVERSED")
    reasons: list[str] = []
    if not age(worker["observed_at_utc"], now, 120):
        reasons.append("STALE_WORKER_OBSERVATION")
    if not age(worker["rate_observed_at_utc"], now, 120):
        reasons.append("STALE_RATE_OBSERVATION")
    if sample < 30:
        reasons.append("INSUFFICIENT_RATE_SAMPLES")
    if worker["source_sha"] != request["source_sha"]:
        reasons.append("SOURCE_DRIFT")
    if worker["model_sha256"] != request["model_sha256"]:
        reasons.append("MODEL_FINGERPRINT_DRIFT")
    if worker["tokenizer_sha256"] != request["tokenizer_sha256"]:
        reasons.append("TOKENIZER_FINGERPRINT_DRIFT")
    if worker["model_identity_verified"] is not True:
        reasons.append("MODEL_IDENTITY_UNVERIFIED")
    if worker["serving_owner_exclusive"] is not True:
        reasons.append("EXCLUSIVE_OWNER_UNVERIFIED")
    if worker["canary_fresh"] is not True:
        reasons.append("CANARY_UNVERIFIED")
    claimed_hit, cache_status = cache_opportunity(
        worker["cache_claim"], worker, request, now
    )
    # Even a matching digest is only a declaration. It never reduces U.
    prompt = request["prompt_tokens"]
    output = request["output_tokens"]
    upper = prompt * prefill + output * decode + qhigh
    lower = (prompt - claimed_hit) * prefill + output * decode + qlow
    require(upper <= MAX_ESTIMATED_US and lower <= upper, "COST_OUT_OF_BOUNDS")
    eligible = not reasons
    return {
        "host": worker["host"],
        "advisory_eligible": eligible,
        "reasons": sorted(reasons),
        "optimistic_lower_us": lower if eligible else None,
        "cache_free_upper_us": upper if eligible else None,
        "unsigned_conditional_hit_tokens": claimed_hit if eligible else 0,
        "cache_claim_state": cache_status,
        "source_identity_independently_attested": False,
        "cache_receipt_signature_verified": False,
        "cost_basis": "DECLARED_RATES_AND_QUEUE_INTERVAL_NOT_MEASURED_SLO",
    }


def assess(observation: dict, *, now: datetime) -> dict:
    """Produce a deterministic non-executable certificate, never route a turn."""
    fields(observation, ROOT_FIELDS, "OBSERVATION_FIELDS")
    require(observation["schema"] == INPUT_SCHEMA, "SCHEMA_MISMATCH")
    require(isinstance(now, datetime) and now.tzinfo is not None
            and now.utcoffset() is not None, "CLOCK_REQUIRED")
    now = now.astimezone(timezone.utc)
    require(age(observation["observed_at_utc"], now, 120), "STALE_OBSERVATION")
    source = digest(observation["protected_main_sha"], SOURCE_SHA, "SOURCE_REQUIRED")
    request = fields(observation["request"], REQUEST_FIELDS, "REQUEST_FIELDS")
    require(type(request["request_id"]) is str and
            REQUEST_ID.fullmatch(request["request_id"]) is not None,
            "REQUEST_ID_INVALID")
    digest(request["source_sha"], SOURCE_SHA, "REQUEST_SOURCE")
    for key in ("model_sha256", "tokenizer_sha256", "prefix_sha256"):
        digest(request[key], DIGEST, "REQUEST_DIGEST")
    require(request["source_sha"] == source, "SOURCE_NOT_PROTECTED_MAIN")
    require(request["classification"] in ("PUBLIC", "INTERNAL"),
            "CLASSIFICATION_NOT_ADMITTED_FOR_ADVISORY")
    require(request["cache_layout"] == "STANDARD_FULL_ATTENTION",
            "UNSUPPORTED_KV_LAYOUT_MLA_OR_HYBRID")
    integer(request["prompt_tokens"], 1, 131072, "PROMPT_TOKENS_INVALID")
    integer(request["output_tokens"], 1, 32768, "OUTPUT_TOKENS_INVALID")
    require(request["prompt_tokens"] + request["output_tokens"] <= 131072,
            "CONTEXT_EXCEEDS_BOUND")
    workers = observation["workers"]
    require(type(workers) is list and len(workers) == 2, "TWO_WORKERS_REQUIRED")
    rows = [inspect_worker(w, request, now) for w in workers]
    require({r["host"] for r in rows} == HOSTS, "DUPLICATE_OR_MISSING_WORKER")
    rows.sort(key=lambda r: r["host"])
    eligible = [r for r in rows if r["advisory_eligible"]]
    robust = False
    regret = None
    candidate = None
    minimax_reference = None
    if not eligible:
        state = "HOLD_NO_ADVISORY_ELIGIBLE_WORKER"
    elif len(eligible) == 1:
        state = "SINGLE_CANDIDATE_REQUIRES_OWNER_ADMISSION"
        candidate = eligible[0]["host"]
        minimax_reference = eligible[0]["host"]
    else:
        # Minimax; cache claims never improve the upper cost.
        # Do not let an unsigned cache claim influence even the tie-break.
        # The lower bound is evidence of uncertainty, not a routing input.
        winner = min(eligible, key=lambda r: (
            r["cache_free_upper_us"], r["host"]
        ))
        minimax_reference = winner["host"]
        alternative = next(r for r in eligible if r["host"] != winner["host"])
        difference = winner["cache_free_upper_us"] - alternative["optimistic_lower_us"]
        regret = max(0, difference)
        robust = difference < 0
        if robust:
            candidate = winner["host"]
            state = "ROBUST_DOMINANCE_CANDIDATE_REQUIRES_OWNER_ADMISSION"
        else:
            state = "HOLD_INTERVAL_OVERLAP_NO_SAFE_PERFORMANCE_RANK"
    body = {
        "schema": SCHEMA,
        "state": state,
        "request_id": request["request_id"],
        "protected_main_sha_supplied_not_verified": source,
        "worker_intervals": rows,
        "advisory_candidate_host": candidate,
        "minimax_reference_host_not_route": minimax_reference,
        "robust_dominance": robust,
        "estimated_regret_upper_us": regret,
        "estimate_not_measured_end_to_end_latency": True,
        "queue_bounds_include_active_work_no_double_charge": True,
        "uncertainty_credit_is_conditional_only": True,
        "source_identity_independently_attested": False,
        "cache_receipt_signature_verified": False,
        "slo_met": None,
        "inference_executed": False,
        "training_performed": False,
        "execution_authorized": False,
        "route_changes_authorized": False,
        "production_ready": False,
        "cache_transfer_assumed": False,
        "cross_gpu_vram_fused": False,
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"),
                         allow_nan=False, ensure_ascii=True).encode("ascii")
    return {**body, "structural_sha256_not_signature":
            hashlib.sha256(DOMAIN + encoded).hexdigest()}