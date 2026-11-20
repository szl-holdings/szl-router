#!/usr/bin/env python3
"""Offline consumer for signed Hugging Face publication/runtime parity evidence.

This verifies a trusted DSSE signature over the unchanged, canonical JSON evidence
produced by ``hf_space_drift_check.py``. It performs no network, Git, deployment,
or inference operation. Acceptance concerns only the historical observation in
the signed payload; it is not live readback or authorization for paid inference.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__:
    from .hf_space_drift_check import (
        EVIDENCE_SCHEMA, SOURCE_PATH, SOURCE_REPOSITORY, new_evidence,
    )
else:
    from hf_space_drift_check import (
        EVIDENCE_SCHEMA, SOURCE_PATH, SOURCE_REPOSITORY, new_evidence,
    )

_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REPO_ID = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ENVELOPE_FIELDS = {
    "payloadType", "payload", "signature", "signed", "organ", "keyid",
    "digest", "algo",
}
_ATTEMPT_FIELDS = {
    "attempt", "observed_at", "state", "code", "retryable", "provider_stage",
    "hf_revision",
}
_FILE_FIELDS = {"path", "source_sha256", "publication_sha256", "state"}


class EvidenceVerificationFailure(ValueError):
    """A sanitized, machine-classifiable offline verification failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> None:
    raise EvidenceVerificationFailure(code, message)


def _json_object(raw: bytes, *, context: str) -> dict[str, object]:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                _fail("DUPLICATE_JSON_KEY", f"{context} contains a duplicate JSON key.")
            result[key] = value
        return result

    def invalid_constant(_value: str) -> None:
        raise ValueError("non-finite JSON number")

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=invalid_constant,
        )
    except EvidenceVerificationFailure:
        raise
    except (UnicodeError, ValueError, TypeError) as exc:
        raise EvidenceVerificationFailure(
            "MALFORMED_JSON", f"{context} is not valid UTF-8 JSON."
        ) from exc
    if type(value) is not dict:
        _fail("MALFORMED_JSON", f"{context} root must be a JSON object.")
    return value


def _exact_sha(value: object, *, field: str, digest: bool = False) -> str:
    pattern = _SHA256 if digest else _SHA
    size = 64 if digest else 40
    if type(value) is not str or not pattern.fullmatch(value):
        _fail("INVALID_VERIFICATION_INPUT", f"{field} must be exactly {size} lowercase hex characters.")
    return value


def _shape(value: object, template: object, *, path: str = "evidence") -> None:
    if type(template) is dict:
        if type(value) is not dict or set(value) != set(template):
            _fail("EVIDENCE_CONTRACT_REJECTED", f"{path} does not match the exact evidence schema.")
        for key, child in template.items():
            _shape(value[key], child, path=f"{path}.{key}")
        return
    if type(template) is list:
        if type(value) is not list:
            _fail("EVIDENCE_CONTRACT_REJECTED", f"{path} must be an array.")
        return
    if template is None:
        return
    if type(value) is not type(template):
        _fail("EVIDENCE_CONTRACT_REJECTED", f"{path} has the wrong JSON type.")


def _expect(container: dict[str, object], key: str, expected: object, *, code: str = "EVIDENCE_CONTRACT_REJECTED") -> None:
    actual = container.get(key)
    if type(actual) is not type(expected) or actual != expected:
        _fail(code, f"Evidence field {key} does not match the required value.")


def _string(container: dict[str, object], key: str) -> str:
    value = container.get(key)
    if type(value) is not str or not value:
        _fail("EVIDENCE_CONTRACT_REJECTED", f"Evidence field {key} must be a non-empty string.")
    return value


def _timestamp(value: object, *, field: str) -> datetime:
    if type(value) is not str or not value:
        _fail("EVIDENCE_TIMESTAMP_INVALID", f"{field} must be a timezone-aware timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceVerificationFailure(
            "EVIDENCE_TIMESTAMP_INVALID", f"{field} is not a valid timestamp."
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail("EVIDENCE_TIMESTAMP_INVALID", f"{field} must include a timezone.")
    return parsed.astimezone(timezone.utc)


def _validate_files(publication: dict[str, object]) -> None:
    files = publication["files"]
    assert isinstance(files, list)
    paths: set[str] = set()
    for entry in files:
        if type(entry) is not dict or set(entry) != _FILE_FIELDS:
            _fail("EVIDENCE_CONTRACT_REJECTED", "A publication file entry has an invalid shape.")
        for key in _FILE_FIELDS:
            _string(entry, key)
        path = entry["path"]
        assert isinstance(path, str)
        if path in paths:
            _fail("EVIDENCE_CONTRACT_REJECTED", "Publication file entries contain a duplicate path.")
        paths.add(path)
        _expect(entry, "state", "ALIGNED")
        source_digest = _exact_sha(entry["source_sha256"], field="Source artifact digest", digest=True)
        publication_digest = _exact_sha(
            entry["publication_sha256"], field="Published artifact digest", digest=True,
        )
        if source_digest != publication_digest:
            _fail("ARTIFACT_DIGEST_MISMATCH", "A source and published artifact digest differ.")

    for key in ("expected_files", "observed_files", "files_checked"):
        if type(publication[key]) is not int:
            _fail("EVIDENCE_CONTRACT_REJECTED", f"Publication field {key} must be an integer.")
    if publication["missing_files"] != [] or publication["unexpected_files"] != []:
        _fail("EVIDENCE_CONTRACT_REJECTED", "Accepted evidence cannot contain publication drift entries.")
    if (
        publication["files_checked"] != len(files)
        or publication["expected_files"] != len(files) + 1
        or publication["observed_files"] != publication["expected_files"]
    ):
        _fail("EVIDENCE_CONTRACT_REJECTED", "Publication file counts are inconsistent with exact output evidence.")


def _validate_attempts(
    attempts: list[object], *, expected_hf_revision: str, started: datetime, observed: datetime,
) -> None:
    if not attempts:
        _fail("EVIDENCE_CONTRACT_REJECTED", "Accepted evidence must include its terminal attempt.")
    for index, attempt in enumerate(attempts):
        if type(attempt) is not dict or set(attempt) != _ATTEMPT_FIELDS:
            _fail("EVIDENCE_CONTRACT_REJECTED", "An evidence attempt has an invalid shape.")
        if type(attempt["attempt"]) is not int or attempt["attempt"] != index + 1:
            _fail("EVIDENCE_CONTRACT_REJECTED", "Evidence attempt numbering is invalid.")
        if type(attempt["retryable"]) is not bool:
            _fail("EVIDENCE_CONTRACT_REJECTED", "Evidence attempt retryable must be a boolean.")
        attempt_observed = _timestamp(attempt["observed_at"], field="attempt.observed_at")
        if not started <= attempt_observed <= observed:
            _fail("EVIDENCE_TIMESTAMP_INVALID", "An evidence attempt is outside the observation interval.")
        terminal = index == len(attempts) - 1
        if terminal:
            _expect(attempt, "state", "ACCEPTED")
            _expect(attempt, "code", None)
            _expect(attempt, "retryable", False)
            _expect(attempt, "provider_stage", "RUNNING")
            _expect(attempt, "hf_revision", expected_hf_revision, code="PUBLISHER_DRIFT")
        else:
            _expect(attempt, "state", "REJECTED")
            _string(attempt, "code")
            if attempt["provider_stage"] is not None:
                _string(attempt, "provider_stage")
            if attempt["hf_revision"] is not None:
                _exact_sha(attempt["hf_revision"], field="Attempt Hugging Face revision")


def _validate_evidence(
    evidence: dict[str, object],
    *,
    expected_source_revision: str,
    expected_hf_revision: str,
    expected_space_id: str,
    max_observation_age_seconds: float,
    now: datetime,
) -> str:
    _shape(evidence, new_evidence())
    _expect(evidence, "schema", EVIDENCE_SCHEMA)
    _expect(evidence, "verdict", "ACCEPTED", code="EVIDENCE_NOT_ACCEPTED")
    _expect(evidence, "authority_state", "READ_ONLY")
    _expect(evidence, "failure", None)
    if type(evidence["provider_auth"]) is not str or evidence["provider_auth"] not in {"ANONYMOUS", "TOKEN"}:
        _fail("EVIDENCE_CONTRACT_REJECTED", "Evidence provider authentication state is invalid.")

    source = evidence["source"]
    publication = evidence["publication"]
    deployment = evidence["deployment"]
    runtime = evidence["runtime"]
    bindings = evidence["bindings"]
    truth = evidence["truth_separation"]
    assert all(isinstance(section, dict) for section in (source, publication, deployment, runtime, bindings, truth))

    _expect(source, "repository", SOURCE_REPOSITORY)
    _expect(source, "path", SOURCE_PATH)
    _expect(source, "revision", expected_source_revision, code="SOURCE_AHEAD")
    _expect(source, "checkout_revision", expected_source_revision, code="SOURCE_CHECKOUT_MISMATCH")
    _expect(source, "state", "EXACT_CHECKOUT")

    _expect(publication, "provider", "HUGGING_FACE")
    _expect(publication, "space", expected_space_id, code="SPACE_ID_MISMATCH")
    _expect(publication, "revision", expected_hf_revision, code="PUBLISHER_DRIFT")
    _expect(publication, "revision_state", "EXACT_STABLE")
    _expect(publication, "source_binding_state", "ALIGNED")
    _expect(publication, "bytes_state", "ALIGNED")
    _expect(publication, "output_set_state", "EXACT")
    _validate_files(publication)

    _expect(deployment, "endpoint_binding_state", "PROVIDER_MATCHED")
    _expect(deployment, "provider_stage", "RUNNING", code="PROVIDER_STAGE_NOT_RUNNING")
    _expect(deployment, "runtime_revision", expected_hf_revision, code="RUNTIME_REVISION_MISMATCH")
    _expect(deployment, "stage_state", "ACCEPTED")
    _string(deployment, "endpoint")

    _expect(runtime, "attested_source_revision", expected_source_revision, code="SOURCE_AHEAD")
    _expect(runtime, "attested_hf_revision", expected_hf_revision, code="RUNTIME_REVISION_MISMATCH")
    _expect(runtime, "readiness_http_status", 200)
    _expect(runtime, "readiness_state", "READY")
    _expect(runtime, "redirect_policy", "REJECT")
    _expect(runtime, "cache_policy_state", "NO_STORE_FRESH")
    _expect(runtime, "witness_state", "SOURCE_AND_HF_REVISION_BOUND")
    for key in ("readiness_url", "source_attestation_url", "witness_nonce"):
        _string(runtime, key)

    _expect(bindings, "source_to_publication", "EXACT", code="BINDING_NOT_EXACT")
    _expect(bindings, "publication_to_runtime", "EXACT", code="BINDING_NOT_EXACT")
    for key, expected in {
        "source_state": "EXACT_CHECKOUT",
        "publication_state": "EXACT_REVISION_ALIGNED",
        "deployment_state": "PROVIDER_RUNNING",
        "runtime_state": "READY_WITNESSED",
        "acceptance_state": "ACCEPTED",
    }.items():
        _expect(truth, key, expected, code="TRUTH_SEPARATION_REJECTED")

    started = _timestamp(evidence["started_at"], field="started_at")
    observed = _timestamp(evidence["observed_at"], field="observed_at")
    attested = _timestamp(runtime["attestation_observed_at"], field="attestation_observed_at")
    if not started <= attested <= observed:
        _fail("EVIDENCE_TIMESTAMP_INVALID", "Runtime attestation is outside the evidence observation interval.")
    age = (now - observed).total_seconds()
    if age < 0 or age > max_observation_age_seconds:
        _fail("EVIDENCE_STALE", "Signed evidence is outside the permitted observation freshness window.")
    _validate_attempts(
        evidence["attempts"],
        expected_hf_revision=expected_hf_revision,
        started=started,
        observed=observed,
    )
    return evidence["observed_at"]


def _receipt_library():
    try:
        import szl_receipt
    except ImportError as exc:
        raise EvidenceVerificationFailure(
            "VERIFIER_UNAVAILABLE", "szl-receipt-dsse is required to verify signed evidence."
        ) from exc
    return szl_receipt


def _public_key_fingerprint(public_key_pem: bytes | str) -> str:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        key = serialization.load_pem_public_key(public_key_pem)
        if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
            raise ValueError("not an ECDSA P-256 public key")
        der = key.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    except Exception as exc:
        raise EvidenceVerificationFailure(
            "SIGNER_KEY_INVALID", "The trusted signing public key is not a valid ECDSA P-256 PEM key."
        ) from exc
    return hashlib.sha256(der).hexdigest()


def verify_signed_evidence(
    envelope_bytes: bytes,
    public_key_pem: bytes | str,
    *,
    expected_source_revision: str,
    expected_hf_revision: str,
    expected_space_id: str,
    max_observation_age_seconds: float,
    expected_signer_key_sha256: str | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Verify one signed envelope or raise ``EvidenceVerificationFailure``."""

    source_revision = _exact_sha(expected_source_revision, field="Expected source revision")
    hf_revision = _exact_sha(expected_hf_revision, field="Expected Hugging Face revision")
    if type(expected_space_id) is not str or not _REPO_ID.fullmatch(expected_space_id):
        _fail("INVALID_VERIFICATION_INPUT", "Expected Space ID must be an explicit owner/name value.")
    if (
        type(max_observation_age_seconds) not in {int, float}
        or not math.isfinite(max_observation_age_seconds)
        or max_observation_age_seconds <= 0
    ):
        _fail("INVALID_VERIFICATION_INPUT", "Maximum observation age must be a finite positive number.")
    if expected_signer_key_sha256 is not None:
        _exact_sha(expected_signer_key_sha256, field="Expected signer key fingerprint", digest=True)
    if now is None:
        now = datetime.now(timezone.utc)
    elif not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        _fail("INVALID_VERIFICATION_INPUT", "Verification time must be timezone-aware.")
    else:
        now = now.astimezone(timezone.utc)
    if type(envelope_bytes) is not bytes:
        _fail("MALFORMED_JSON", "Signed envelope input must be bytes.")

    envelope = _json_object(envelope_bytes, context="Signed envelope")
    if "signed" in envelope and envelope["signed"] is not True:
        _fail("UNSIGNED_EVIDENCE", "Unsigned evidence is never accepted.")
    if set(envelope) != _ENVELOPE_FIELDS:
        _fail("ENVELOPE_CONTRACT_REJECTED", "Signed envelope does not have the exact receipt envelope shape.")
    for key in ("payloadType", "payload", "signature", "organ", "keyid", "digest", "algo"):
        if type(envelope[key]) is not str:
            _fail("ENVELOPE_CONTRACT_REJECTED", f"Envelope field {key} must be a string.")
    _expect(envelope, "algo", "ECDSA-P256-SHA256", code="ENVELOPE_CONTRACT_REJECTED")
    payload_digest = _exact_sha(envelope["digest"], field="Payload digest", digest=True)

    try:
        payload_bytes = base64.b64decode(envelope["payload"], validate=True)
    except Exception as exc:
        raise EvidenceVerificationFailure(
            "ENVELOPE_CONTRACT_REJECTED", "Signed envelope payload is not strict base64."
        ) from exc
    evidence = _json_object(payload_bytes, context="Signed evidence payload")
    actual_digest = hashlib.sha256(payload_bytes).hexdigest()
    if payload_digest != actual_digest:
        _fail("ARTIFACT_DIGEST_MISMATCH", "Envelope digest does not match the canonical signed payload.")

    signer_fingerprint = _public_key_fingerprint(public_key_pem)
    if expected_signer_key_sha256 is not None and signer_fingerprint != expected_signer_key_sha256:
        _fail("SIGNER_IDENTITY_MISMATCH", "Trusted public key does not match the expected signer identity.")

    receipt = _receipt_library()
    _expect(envelope, "payloadType", receipt.PAYLOAD_TYPE, code="ENVELOPE_CONTRACT_REJECTED")
    try:
        valid, detail = receipt.verify_receipt(envelope, public_key_pem=public_key_pem)
    except Exception as exc:
        raise EvidenceVerificationFailure(
            "SIGNATURE_VERIFICATION_FAILED", "The receipt library could not verify the signature."
        ) from exc
    if not valid:
        code = "UNSIGNED_EVIDENCE" if detail == "unsigned-honest" else "SIGNATURE_VERIFICATION_FAILED"
        _fail(code, "The signed evidence envelope failed cryptographic verification.")

    observed_at = _validate_evidence(
        evidence,
        expected_source_revision=source_revision,
        expected_hf_revision=hf_revision,
        expected_space_id=expected_space_id,
        max_observation_age_seconds=max_observation_age_seconds,
        now=now,
    )
    return {
        "verdict": "ACCEPTED",
        "acceptance_scope": "OFFLINE_HISTORICAL_PARITY_OBSERVATION",
        "live_readback": "NOT_PERFORMED",
        "deployment_authorized": False,
        "paid_inference_authorized": False,
        "source_revision": source_revision,
        "hf_revision": hf_revision,
        "space": expected_space_id,
        "observed_at": observed_at,
        "payload_sha256": actual_digest,
        "signer_key_sha256": signer_fingerprint,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--envelope", type=Path, required=True, help="Signed receipt envelope JSON file")
    parser.add_argument("--public-key", type=Path, required=True, help="Explicitly trusted ECDSA P-256 public key PEM")
    parser.add_argument("--expected-source-revision", required=True, help="Exact protected GitHub source 40-hex SHA")
    parser.add_argument("--expected-hf-revision", required=True, help="Exact expected Hugging Face head 40-hex SHA")
    parser.add_argument("--expected-space-id", required=True, help="Exact Hugging Face owner/name Space ID")
    parser.add_argument(
        "--max-observation-age-seconds", type=float, required=True,
        help="Maximum permitted age of the signed evidence observed_at timestamp",
    )
    parser.add_argument(
        "--expected-signer-key-sha256",
        help="Optional SHA-256 of the trusted public key DER SubjectPublicKeyInfo",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        envelope_bytes = args.envelope.read_bytes()
        public_key_pem = args.public_key.read_bytes()
    except OSError:
        result = {"verdict": "REJECTED", "code": "EVIDENCE_UNAVAILABLE"}
    else:
        try:
            result = verify_signed_evidence(
                envelope_bytes,
                public_key_pem,
                expected_source_revision=args.expected_source_revision,
                expected_hf_revision=args.expected_hf_revision,
                expected_space_id=args.expected_space_id,
                max_observation_age_seconds=args.max_observation_age_seconds,
                expected_signer_key_sha256=args.expected_signer_key_sha256,
            )
        except EvidenceVerificationFailure as exc:
            result = {"verdict": "REJECTED", "code": exc.code, "message": str(exc)}
        except Exception:
            result = {"verdict": "REJECTED", "code": "INTERNAL_VERIFIER_ERROR"}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["verdict"] == "ACCEPTED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
