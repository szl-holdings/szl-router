"""Shared Hugging Face runtime state machine for SZL Router.

Classification order:
1. Validate repository identity.
2. Validate visibility metadata.
3. Validate stage type and syntax.
4. Detect terminal stages before requiring a runtime SHA.
5. Detect transition stages.
6. Validate non-empty SHA values strictly.
7. Compare runtime SHA to repository SHA.
8. Declare RUNNING_ALIGNED only when every required equality holds.
"""
from __future__ import annotations

import re
from typing import Any

CANONICAL_REPO_ID = "SZLHOLDINGS/llm-router-live"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
STAGE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

TRANSITION_STAGES = {
    "APP_STARTING",
    "BUILDING",
    "PENDING",
    "QUEUED",
    "RESTARTING",
    "RUNNING_APP_STARTING",
    "RUNNING_BUILDING",
    "STARTING",
    "METADATA_PENDING",
}
TERMINAL_STAGES = {
    "BUILD_ERROR",
    "RUNTIME_ERROR",
    "CONFIG_ERROR",
    "PAUSED",
    "STOPPED",
    "ERROR",
}

RETRYABLE_HTTP = {404, 408, 425, 429, 500, 502, 503, 504}
FAIL_CLOSED_HTTP = {401, 403}


class ContractError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = details or {}
        self.state = code


def reject_alternate_target(repo_id: str) -> None:
    if repo_id != CANONICAL_REPO_ID:
        raise ContractError(
            "TARGET_REJECTED",
            "Only SZLHOLDINGS/llm-router-live is admitted.",
            retryable=False,
            details={"repo_id": repo_id},
        )


def _sha_or_none(value: object) -> str | None:
    if value is None or value == "":
        return None
    candidate = str(value).strip().lower()
    if not SHA_RE.fullmatch(candidate):
        raise ContractError(
            "INVALID_RESPONSE",
            "Non-empty revision is not an exact 40-character Git SHA.",
            retryable=False,
        )
    return candidate


def classify(info: dict[str, Any], runtime: dict[str, Any], *, expected_repo_id: str = CANONICAL_REPO_ID) -> dict[str, Any]:
    reject_alternate_target(expected_repo_id)

    identity = str(info.get("id") or info.get("name") or expected_repo_id)
    if identity and identity != CANONICAL_REPO_ID and info.get("id"):
        raise ContractError(
            "INVALID_RESPONSE",
            "Provider identity does not match the canonical Router Space.",
            retryable=False,
        )

    private = info.get("private")
    if private is None:
        return {"state": "VISIBILITY_PENDING", "retryable": True}
    if not isinstance(private, bool):
        raise ContractError("INVALID_RESPONSE", "Visibility is not boolean.", retryable=False)
    if private:
        return {"state": "VISIBILITY_PENDING", "retryable": True}

    publication = _sha_or_none(info.get("sha"))
    stage_value = runtime.get("stage")
    if stage_value is None or stage_value == "":
        return {
            "state": "METADATA_PENDING",
            "retryable": True,
            "publication_revision": publication,
        }
    if not isinstance(stage_value, str):
        raise ContractError("INVALID_RESPONSE", "Stage is not a string.", retryable=False)
    stage = stage_value.strip().upper()
    if not STAGE_RE.fullmatch(stage):
        raise ContractError("INVALID_RESPONSE", "Stage is malformed.", retryable=False)

    if stage in TERMINAL_STAGES:
        raise ContractError(
            stage if stage in {"BUILD_ERROR", "RUNTIME_ERROR", "CONFIG_ERROR"} else "RUNTIME_ERROR",
            f"Terminal provider stage {stage}.",
            retryable=False,
            details={"stage": stage, "runtime_sha": runtime.get("sha")},
        )

    runtime_sha = _sha_or_none(runtime.get("sha"))
    if stage in TRANSITION_STAGES:
        return {
            "state": stage if stage != "METADATA_PENDING" else "BUILDING",
            "retryable": True,
            "publication_revision": publication,
            "runtime_revision": runtime_sha,
        }

    if publication is None:
        return {"state": "METADATA_PENDING", "retryable": True}

    if stage != "RUNNING":
        raise ContractError(
            "INVALID_RESPONSE",
            f"Unknown non-empty stage {stage}.",
            retryable=False,
            details={"stage": stage},
        )

    if runtime_sha is None:
        return {
            "state": "METADATA_PENDING",
            "retryable": True,
            "publication_revision": publication,
        }
    if runtime_sha != publication:
        return {
            "state": "RUNNING_STALE",
            "retryable": True,
            "publication_revision": publication,
            "runtime_revision": runtime_sha,
        }
    return {
        "state": "RUNNING_ALIGNED",
        "retryable": False,
        "publication_revision": publication,
        "runtime_revision": runtime_sha,
        "stage": "RUNNING",
        "private": False,
    }
