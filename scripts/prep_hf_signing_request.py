#!/usr/bin/env python3
"""Prepare an owner-review request for accepted HF parity evidence.

This component only reads explicit local input, validates the existing evidence
contract, and derives original-byte and canonical DSSE-payload digests. It has no
private-key input, never signs, and performs no network, Git, or deploy operation.
Expected source/HF revisions are operator-supplied expectations, not observations.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

if __package__:
    from . import hf_space_drift_check as drift
    from . import verify_hf_release_evidence as consumer
else:
    import hf_space_drift_check as drift
    import verify_hf_release_evidence as consumer

REQUEST_SCHEMA = "szl.hf-parity-signing-request/v1"
RECEIPT_KIND = "hf.publication-runtime-parity"
EXPECTATION_PROVENANCE = "OPERATOR_SUPPLIED_NOT_OBSERVED_BY_THIS_TOOL"
INSTRUCTIONS = (
    "Review the original accepted evidence and all operator-supplied expectations. "
    "Decode canonical_evidence_body_base64 and verify canonical_evidence_body_sha256. "
    "An owner-approved signer must sign only that canonical evidence body, not the request "
    "wrapper, as szl_receipt.Receipt(receipt_kind, json.loads(body)) with "
    "szl_receipt.sign_receipt. Confirm the envelope payload and digest match the request, "
    "then verify the envelope with scripts/verify_hf_release_evidence.py and the separately "
    "pinned corresponding public key. This request has no signature or verification authority."
)


def _canonical_json(evidence: dict[str, object]) -> bytes:
    try:
        from szl_receipt._canonical import canonical_json
    except ImportError as exc:
        raise consumer.EvidenceVerificationFailure(
            "VERIFIER_UNAVAILABLE",
            "szl-receipt-dsse is required to derive the canonical DSSE evidence body.",
        ) from exc
    try:
        return canonical_json(evidence)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise consumer.EvidenceVerificationFailure(
            "MALFORMED_JSON", "Evidence cannot be represented as finite canonical JSON."
        ) from exc


def prepare_signing_request(
    evidence_bytes: bytes,
    *,
    expected_source_revision: str,
    expected_hf_revision: str,
    expected_space_id: str,
    max_observation_age_seconds: float,
    now: datetime,
) -> dict[str, object]:
    """Validate accepted evidence and return an unsigned owner-review request."""

    source_revision = consumer._exact_sha(
        expected_source_revision, field="Expected source revision"
    )
    hf_revision = consumer._exact_sha(
        expected_hf_revision, field="Expected Hugging Face revision"
    )
    if type(expected_space_id) is not str or not consumer._REPO_ID.fullmatch(
        expected_space_id
    ):
        consumer._fail(
            "INVALID_VERIFICATION_INPUT",
            "Expected Space ID must be an explicit owner/name value.",
        )
    if (
        type(max_observation_age_seconds) not in {int, float}
        or not math.isfinite(max_observation_age_seconds)
        or max_observation_age_seconds <= 0
    ):
        consumer._fail(
            "INVALID_VERIFICATION_INPUT",
            "Maximum observation age must be a finite positive number.",
        )
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        consumer._fail(
            "INVALID_VERIFICATION_INPUT", "Current time must be timezone-aware."
        )
    now = now.astimezone(timezone.utc)
    if type(evidence_bytes) is not bytes:
        consumer._fail("MALFORMED_JSON", "Source evidence input must be bytes.")

    evidence = consumer._json_object(evidence_bytes, context="Source evidence")
    observed_at = consumer._validate_evidence(
        evidence,
        expected_source_revision=source_revision,
        expected_hf_revision=hf_revision,
        expected_space_id=expected_space_id,
        max_observation_age_seconds=max_observation_age_seconds,
        now=now,
    )
    body = _canonical_json(evidence)
    body_digest = hashlib.sha256(body).hexdigest()
    return {
        "schema": REQUEST_SCHEMA,
        "authority": "OWNER_REVIEW_REQUIRED",
        "source_evidence_sha256": hashlib.sha256(evidence_bytes).hexdigest(),
        "source_evidence_byte_length": len(evidence_bytes),
        "canonical_evidence_body_base64": base64.b64encode(body).decode("ascii"),
        "canonical_evidence_body_sha256": body_digest,
        "canonical_evidence_body_byte_length": len(body),
        "receipt_kind": RECEIPT_KIND,
        "source_repository": drift.SOURCE_REPOSITORY,
        "source_path": drift.SOURCE_PATH,
        "expected_source_revision": source_revision,
        "expected_hf_revision": hf_revision,
        "expected_space_id": expected_space_id,
        "expectation_provenance": EXPECTATION_PROVENANCE,
        "evidence_observed_at": observed_at,
        "request_prepared_at": now.isoformat().replace("+00:00", "Z"),
        "max_observation_age_seconds": max_observation_age_seconds,
        "signature": None,
        "signing_verified": False,
        "instructions": INSTRUCTIONS,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence", type=Path, required=True, help="Accepted producer JSON file"
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="New signing-request JSON file"
    )
    parser.add_argument("--expected-source-revision", required=True)
    parser.add_argument("--expected-hf-revision", required=True)
    parser.add_argument("--expected-space-id", required=True)
    parser.add_argument("--max-observation-age-seconds", type=float, required=True)
    parser.add_argument(
        "--current-timestamp",
        required=True,
        help="Explicit timezone-aware ISO-8601 freshness timestamp; not observed by this tool",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        evidence_bytes = args.evidence.read_bytes()
        now = consumer._timestamp(args.current_timestamp, field="current timestamp")
        request = prepare_signing_request(
            evidence_bytes,
            expected_source_revision=args.expected_source_revision,
            expected_hf_revision=args.expected_hf_revision,
            expected_space_id=args.expected_space_id,
            max_observation_age_seconds=args.max_observation_age_seconds,
            now=now,
        )
        request_bytes = (
            json.dumps(request, indent=2, sort_keys=True, allow_nan=False) + "\n"
        ).encode("utf-8")
        with args.output.open("xb") as output:
            output.write(request_bytes)
    except consumer.EvidenceVerificationFailure as exc:
        result = {"verdict": "REJECTED", "code": exc.code, "message": str(exc)}
    except OSError:
        result = {
            "verdict": "REJECTED",
            "code": "SIGNING_REQUEST_IO_ERROR",
            "message": "An explicit input/output file could not be read or created without overwrite.",
        }
    except Exception:  # noqa: BLE001 - unexpected CLI errors must fail closed and sanitized
        result = {"verdict": "REJECTED", "code": "INTERNAL_PREPARATION_ERROR"}
    else:
        result = {
            "verdict": "PREPARED",
            "schema": REQUEST_SCHEMA,
            "source_evidence_sha256": request["source_evidence_sha256"],
            "canonical_evidence_body_sha256": request["canonical_evidence_body_sha256"],
        }
    print(json.dumps(result, sort_keys=True))
    return 0 if result["verdict"] == "PREPARED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
