"""Pure, fail-closed SaladCloud/Massed capacity coexistence advisory.

This is NOT a router, scheduler, source of money, provider API client or
admission token. No imports from active app and no network or mutation.
Separate pools: Massed for pinned inference, Salad for existing interruptible
training/evaluation. Never conflate group states with trainer liveness.

Published research inspirations (concepts only, no third-party code):
- vLLM PagedAttention (Kwon et al, SOSP 2023): block-allocated KV cache.
- SGLang RadixAttention (Zheng et al, NeurIPS 2024): cache-aware affinity.
- NVIDIA Dynamo (2026): disaggregated prefill/decode only after measurement.
- llm-d (Oct 2026): multi-tenant, SLO and source-aware routing.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import re
from typing import Any, Mapping

SCHEMA = "szl.salad-massed-coexistence-advisory/v1"
INPUT_SCHEMA = "szl.salad-massed-observation/v1"
MODEL_SHA = re.compile(r"[0-9a-f]{64}\Z")
SOURCE_SHA = re.compile(r"[0-9a-f]{40}\Z")
HOSTS = frozenset({"gate1", "cuda"})
CLASSIFICATIONS = frozenset({"PUBLIC", "INTERNAL"})
RESERVE_DISK_BYTES = 50 * 1024**3
GPU_VRAM_RESERVE_MIB = {"gate1": 6000, "cuda": 4000}
MAX_OBSERVATION_AGE_SECONDS = 120
SHAPE_LIMITS = {
    "layers": (1, 256),
    "kv_heads": (1, 256),
    "head_dim": (1, 512),
    "dtype_bytes": (1, 8),
    "block_tokens": (1, 256),
    "context_tokens": (1, 131072),
    "concurrent_sequences": (1, 256),
    "workspace_mib": (0, 32768),
}
EXPECTED_TOP = frozenset(
    {
        "schema",
        "observed_at_utc",
        "protected_main_sha",
        "massed_hosts",
        "salad",
        "gateway",
        "request",
    }
)


class Hold(ValueError):
    """A stable refusal code, never untrusted provider text or URLs."""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise Hold(code)


def integer(value: Any, low: int, high: int, code: str) -> int:
    require(type(value) is int and low <= value <= high, code)
    return value


def timestamp(value: Any) -> datetime:
    require(isinstance(value, str) and len(value) <= 40, "TIMESTAMP_REQUIRED")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise Hold("TIMESTAMP_INVALID") from None
    require(
        parsed.tzinfo is not None and parsed.utcoffset() is not None, "NAIVE_TIMESTAMP"
    )
    return parsed.astimezone(timezone.utc)


def age_seconds(value: Any, now: datetime) -> float:
    observed = timestamp(value)
    delta = (now - observed).total_seconds()
    require(math.isfinite(delta), "AGE_INVALID")
    require(-5 <= delta <= MAX_OBSERVATION_AGE_SECONDS, "STALE_OR_FUTURE_OBSERVATION")
    return round(max(0.0, delta), 3)


def kv_bytes(shape: Mapping[str, Any]) -> dict[str, int]:
    """Conservative *planning* estimate, never a measured residency claim.

    B_KV = 2 * layers * KV_heads * head_dim * bytes_per_element *
           ceil(context_tokens/block_tokens)*block_tokens * sequences.
    Add a declared 10% fragmentation margin and workspace memory.
    Does not account for exact engine tensor layouts, CUDA allocator, RoPE,
    activation peak, prefix-sharing, or network KV-transfer buffers.
    """
    require(
        isinstance(shape, Mapping) and set(shape) == set(SHAPE_LIMITS),
        "KV_SHAPE_FIELDS_REQUIRED",
    )
    v = {
        key: integer(shape[key], *bounds, "INVALID_KV_" + key.upper())
        for key, bounds in SHAPE_LIMITS.items()
    }
    blocks = (v["context_tokens"] + v["block_tokens"] - 1) // v["block_tokens"]
    allocated_tokens = blocks * v["block_tokens"] * v["concurrent_sequences"]
    raw = (
        2
        * v["layers"]
        * v["kv_heads"]
        * v["head_dim"]
        * v["dtype_bytes"]
        * allocated_tokens
    )
    reserved = (raw * 11 + 9) // 10 + v["workspace_mib"] * 2**20
    return {
        "raw_kv_bytes": raw,
        "planned_reserved_bytes": reserved,
        "planned_mib_ceil": (reserved + 2**20 - 1) // 2**20,
        "padded_tokens_across_sequences": allocated_tokens,
        "claim": "PLANNING_ASSUMPTION_NOT_VRAM_MEASUREMENT",
    }


def _exact_keys(value: Any, names: set[str], code: str) -> Mapping[str, Any]:
    require(isinstance(value, Mapping) and set(value) == names, code)
    return value


def _host_observation(
    host: Mapping[str, Any],
    request: Mapping[str, Any],
    estimated_mib: int,
    now: datetime,
) -> dict[str, Any]:
    allowed = {
        "id",
        "observed_at_utc",
        "ready_http",
        "ready",
        "disk_free_bytes",
        "vram_free_mib",
        "boot_id",
        "catalog_boot_id",
        "artifact_fingerprint",
        "model_loaded_identity_verified",
        "canary_fresh",
        "coincident_training_processes",
        "serving_owner_exclusive",
    }
    _exact_keys(host, allowed, "HOST_FIELDS_INCOMPLETE")
    name = host["id"]
    require(name in HOSTS, "UNKNOWN_MASSED_HOST")
    reasons: list[str] = []
    try:
        age = age_seconds(host["observed_at_utc"], now)
    except Hold:
        age = None
        reasons.append("STALE_HOST_OBSERVATION")
    if (
        type(host["ready_http"]) is not int
        or host["ready_http"] != 200
        or host["ready"] is not True
    ):
        reasons.append("MODELHOST_NOT_READY")
    if (
        type(host["disk_free_bytes"]) is not int
        or host["disk_free_bytes"] < RESERVE_DISK_BYTES
    ):
        reasons.append("DISK_BELOW_HARD_RESERVE")
    if type(host["vram_free_mib"]) is not int or host["vram_free_mib"] < (
        GPU_VRAM_RESERVE_MIB[name] + estimated_mib
    ):
        reasons.append("VRAM_AFTER_KV_BELOW_RESERVE")
    if (
        not isinstance(host["boot_id"], str)
        or not host["boot_id"]
        or host["boot_id"] != host["catalog_boot_id"]
    ):
        reasons.append("MODELHOST_BOOT_IDENTITY_MISSING_OR_DRIFT")
    if (
        host["model_loaded_identity_verified"] is not True
        or host["canary_fresh"] is not True
    ):
        reasons.append("MODEL_IDENTITY_OR_CANARY_UNVERIFIED")
    if (
        not isinstance(host["artifact_fingerprint"], str)
        or MODEL_SHA.fullmatch(host["artifact_fingerprint"]) is None
        or host["artifact_fingerprint"] != request["required_artifact_fingerprint"]
    ):
        reasons.append("MODEL_ARTIFACT_FINGERPRINT_MISMATCH")
    if host["coincident_training_processes"] is not False:
        reasons.append("TRAINER_RESOURCE_COLLISION_NOT_EXCLUDED")
    if host["serving_owner_exclusive"] is not True:
        reasons.append("SINGLE_OWNER_LEASE_UNVERIFIED")
    free = host["vram_free_mib"]
    headroom = (
        free - GPU_VRAM_RESERVE_MIB[name] - estimated_mib if type(free) is int else None
    )
    return {
        "host": name,
        "advisory_eligible": not reasons,
        "reasons": sorted(set(reasons)),
        "free_vram_after_reserve_and_plan_mib": headroom,
        "age_seconds": age,
        "estimated_extra_vram_mib": estimated_mib,
        "identity_digest_truncated": None,  # No unnecessary artifact exposure.
        "capacity_rank_basis": "ADDITIONAL_MEMORY_HEADROOM_NOT_THROUGHPUT",
    }


def _salad_observation(salad: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    required = {
        "observed_at_utc",
        "project",
        "groups_returned",
        "groups_captured",
        "other_projects_observed",
        "provider_access_denied",
        "active_replica_count",
        "training_process_liveness_verified",
        "new_spend_authorized",
    }
    _exact_keys(salad, required, "SALAD_SCOPE_REQUIRED")
    require(salad["project"] == "default", "SALAD_PROJECT_SCOPE_UNKNOWN")
    returned = integer(salad["groups_returned"], 0, 10_000, "SALAD_RETURNED_INVALID")
    captured = integer(salad["groups_captured"], 0, returned, "SALAD_CAPTURED_INVALID")
    active = integer(
        salad["active_replica_count"], 0, 10_000, "SALAD_ACTIVE_COUNT_INVALID"
    )
    try:
        age = age_seconds(salad["observed_at_utc"], now)
    except Hold:
        age = None
    full = (
        age is not None
        and captured == returned
        and salad["other_projects_observed"] is True
        and salad["provider_access_denied"] is False
    )
    reasons = []
    if age is None:
        reasons.append("SALAD_OBSERVATION_STALE")
    if captured != returned:
        reasons.append("SALAD_GROUP_TAIL_OMITTED")
    if salad["other_projects_observed"] is not True:
        reasons.append("OTHER_SALAD_PROJECTS_UNOBSERVED")
    if salad["provider_access_denied"] is not False:
        reasons.append("SALAD_PROVIDER_DENIAL_OR_UNKNOWN")
    if salad["training_process_liveness_verified"] is not True:
        reasons.append("TRAINER_PROCESS_LIVENESS_UNVERIFIED")
    if salad["new_spend_authorized"] is not True:
        reasons.append("NO_NEW_SALAD_BUDGET_AUTHORIZATION")
    return {
        "role": "INTERRUPTIBLE_TRAINING_AND_EVALUATION",
        "continuity": "PRESERVE_EXISTING_REPLICAS_AND_JOBS",
        "groups_returned": returned,
        "groups_captured": captured,
        "omitted_groups": returned - captured,
        "active_replicas_in_observed_scope": active,
        "full_capacity_inventory": full,
        "age_seconds": age,
        "reasons": reasons,
        "reallocation_is_safe_to_assume": False,
        "new_replicas_authorized": 0,
        "trainer_checkpoint_consistency_verified": False,
        "automatic_retry_after_unknown_execution": False,
    }


def assess(snapshot: Mapping[str, Any], *, now: datetime) -> dict[str, Any]:
    """Analyze a supplied observation. NEVER dispatch or configure a service."""
    _exact_keys(snapshot, set(EXPECTED_TOP), "SNAPSHOT_FIELDS_REQUIRED")
    require(snapshot["schema"] == INPUT_SCHEMA, "SNAPSHOT_SCHEMA")
    require(now.tzinfo is not None and now.utcoffset() is not None, "CLOCK_REQUIRED")
    now = now.astimezone(timezone.utc)
    age_seconds(snapshot["observed_at_utc"], now)
    source = snapshot["protected_main_sha"]
    require(
        isinstance(source, str) and SOURCE_SHA.fullmatch(source) is not None,
        "PROTECTED_MAIN_SHA_REQUIRED",
    )
    request = _exact_keys(
        snapshot["request"],
        {
            "kind",
            "model_alias",
            "required_artifact_fingerprint",
            "classification",
            "kv_shape",
        },
        "REQUEST_FIELDS_REQUIRED",
    )
    require(
        request["kind"] in {"inference", "training", "evaluation"}, "UNKNOWN_WORKLOAD"
    )
    require(request["classification"] in CLASSIFICATIONS, "DATA_CLASSIFICATION_DENIED")
    require(
        isinstance(request["model_alias"], str)
        # Align with router_control.app.IDENTIFIER: Ollama tag aliases such
        # as khipu:latest are valid public names, never inference authority.
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,95}", request["model_alias"])
        is not None,
        "MODEL_ALIAS_INVALID",
    )
    require(
        isinstance(request["required_artifact_fingerprint"], str)
        and MODEL_SHA.fullmatch(request["required_artifact_fingerprint"]) is not None,
        "PINNED_MODEL_FINGERPRINT_REQUIRED",
    )
    memory = kv_bytes(request["kv_shape"])
    nodes = snapshot["massed_hosts"]
    require(
        isinstance(nodes, list) and len(nodes) == 2,
        "TWO_MASSED_HOST_OBSERVATIONS_REQUIRED",
    )
    results = [
        _host_observation(node, request, memory["planned_mib_ceil"], now)
        for node in nodes
    ]
    require({r["host"] for r in results} == HOSTS, "DUPLICATE_OR_MISSING_MASSED_NODE")
    salad = _salad_observation(snapshot["salad"], now)
    gateway = _exact_keys(
        snapshot["gateway"],
        {
            "cloudflare_access_deny_verified",
            "origin_bypass_closed_verified",
            "router_source_revision_verified",
            "hub_runtime_source_verified",
            "a11oy_router_configuration_present",
            "inference_origin_authenticated",
        },
        "GATEWAY_EVIDENCE_REQUIRED",
    )
    # No mere status or HTTP 200 may satisfy these exact bool admission signals.
    gateway_ok = all(gateway[key] is True for key in gateway)
    eligible = sorted(
        (r for r in results if r["advisory_eligible"]),
        key=lambda r: (-r["free_vram_after_reserve_and_plan_mib"], r["host"]),
    )
    candidate = eligible[0]["host"] if eligible else None
    if request["kind"] != "inference":
        state = "HOLD_NO_NEW_SALAD_JOB_OR_BUDGET_AUTHORITY"
        candidate = None
    elif candidate is None:
        state = "HOLD_MASSED_RESOURCE_OR_IDENTITY"
    elif not gateway_ok:
        state = "HOLD_AUTHENTICATED_GATEWAY_AND_SOURCE_BINDING"
    else:
        state = "CANDIDATE_REQUIRES_SEPARATE_OWNER_RELEASE_ADMISSION"
    return {
        "schema": SCHEMA,
        "state": state,
        "phase": request["kind"],
        "data_classification": request["classification"],
        "model_alias": request["model_alias"],
        "advisory_candidate_host": candidate,
        "massed": results,
        "salad": salad,
        "kv_memory_plan": memory,
        "trusted_gateway_evidence_complete": gateway_ok,
        "source_revision_supplied_not_independently_attested": source,
        "execution_authorized": False,
        "route_changes_authorized": False,
        "salad_job_creation_authorized": False,
        "salad_scale_out_authorized": False,
        "cross_provider_kv_transfer_assumed": False,
        "restarts_authorized": False,
        "paid_compute_authorized": False,
        "model_quality_qualified": False,
        "cloudflare_settlement_qualified": False,
        "latency_rank": "UNAVAILABLE_INSUFFICIENT_FRESH_MULTI_REQUEST_SAMPLE",
        "hashes_are_signatures": False,
        "research_algorithms_implemented": False,
        "allocation_policy": "SEPARATE_POOL_SALAD_TRAIN_EVAL_MASSED_PINNED_INFERENCE",
    }


def input_json(raw: bytes) -> Mapping[str, Any]:
    require(isinstance(raw, bytes) and len(raw) <= 64_000, "INPUT_TOO_LARGE")

    def distinct(pairs):
        result = {}
        for k, v in pairs:
            if k in result:
                raise Hold("DUPLICATE_JSON_KEY")
            result[k] = v
        return result

    try:
        parsed = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=distinct,
            parse_constant=lambda _: (_ for _ in ()).throw(Hold("NONFINITE_JSON")),
        )
    except (UnicodeError, ValueError) as exc:
        if isinstance(exc, Hold):
            raise
        raise Hold("INVALID_UTF8_OR_JSON") from None
    require(isinstance(parsed, dict), "INPUT_OBJECT_REQUIRED")
    return parsed
