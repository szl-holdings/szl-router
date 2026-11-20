"""Hermetic tests for the offline signed HF release-evidence consumer."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import szl_receipt

from scripts import hf_space_drift_check as drift
from scripts import verify_hf_release_evidence as consumer


SOURCE_SHA = "a" * 40
HF_SHA = "b" * 40
OTHER_SOURCE_SHA = "c" * 40
OTHER_HF_SHA = "d" * 40
SPACE_ID = "SZLHOLDINGS/llm-router-live"
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
FILE_DIGEST = "1" * 64
_USE_TEST_KEY = object()


def accepted_evidence(now=NOW):
    def timestamp(value):
        return value.isoformat().replace("+00:00", "Z")

    evidence = drift.new_evidence(
        {
            "provider_auth": "ANONYMOUS",
            "source_repository": drift.SOURCE_REPOSITORY,
            "source_revision": SOURCE_SHA,
            "checkout_revision": SOURCE_SHA,
            "repo_id": SPACE_ID,
            "endpoint": "https://szlholdings-llm-router-live.hf.space",
        }
    )
    evidence.update(
        started_at=timestamp(now - timedelta(seconds=2)),
        observed_at=timestamp(now),
        verdict="ACCEPTED",
    )
    evidence["source"]["state"] = "EXACT_CHECKOUT"
    evidence["publication"].update(
        revision=HF_SHA,
        revision_state="EXACT_STABLE",
        source_binding_state="ALIGNED",
        bytes_state="ALIGNED",
        output_set_state="EXACT",
        expected_files=2,
        observed_files=2,
        files_checked=1,
        files=[
            {
                "path": "index.html",
                "source_sha256": FILE_DIGEST,
                "publication_sha256": FILE_DIGEST,
                "state": "ALIGNED",
            }
        ],
    )
    evidence["deployment"].update(
        endpoint_binding_state="PROVIDER_MATCHED",
        provider_stage="RUNNING",
        runtime_revision=HF_SHA,
        stage_state="ACCEPTED",
    )
    evidence["runtime"].update(
        readiness_http_status=200,
        readiness_state="READY",
        attested_source_revision=SOURCE_SHA,
        attested_hf_revision=HF_SHA,
        attestation_observed_at=timestamp(now - timedelta(seconds=1)),
        cache_policy_state="NO_STORE_FRESH",
        witness_nonce="test-only-witness-nonce",
        witness_state="SOURCE_AND_HF_REVISION_BOUND",
    )
    evidence["bindings"] = {
        "source_to_publication": "EXACT",
        "publication_to_runtime": "EXACT",
    }
    evidence["truth_separation"] = {
        "source_state": "EXACT_CHECKOUT",
        "publication_state": "EXACT_REVISION_ALIGNED",
        "deployment_state": "PROVIDER_RUNNING",
        "runtime_state": "READY_WITNESSED",
        "acceptance_state": "ACCEPTED",
    }
    evidence["attempts"] = [
        {
            "attempt": 1,
            "observed_at": timestamp(now),
            "state": "ACCEPTED",
            "code": None,
            "retryable": False,
            "provider_stage": "RUNNING",
            "hf_revision": HF_SHA,
        }
    ]
    return evidence


class SignedEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.private_key, self.public_key = szl_receipt.generate_keypair()

    def envelope(self, evidence=None, *, private_key=_USE_TEST_KEY, organ="test-only"):
        return szl_receipt.sign_receipt(
            szl_receipt.Receipt("hf.publication-runtime-parity", evidence or accepted_evidence()),
            self.private_key if private_key is _USE_TEST_KEY else private_key,
            organ=organ,
            keyid="unauthenticated-display-label",
        )

    @staticmethod
    def encoded(envelope):
        return json.dumps(envelope, sort_keys=True).encode("utf-8")

    def verify(self, envelope, **overrides):
        arguments = {
            "expected_source_revision": SOURCE_SHA,
            "expected_hf_revision": HF_SHA,
            "expected_space_id": SPACE_ID,
            "max_observation_age_seconds": 300,
            "now": NOW,
        }
        arguments.update(overrides)
        return consumer.verify_signed_evidence(
            self.encoded(envelope), self.public_key, **arguments
        )

    def assert_rejected(self, code, envelope, **overrides):
        with self.assertRaises(consumer.EvidenceVerificationFailure) as caught:
            self.verify(envelope, **overrides)
        self.assertEqual(code, caught.exception.code)

    def resigned(self, evidence):
        return self.envelope(evidence)

    def test_valid_signed_fixture_accepts_only_offline_observation(self):
        fingerprint = consumer._public_key_fingerprint(self.public_key)
        result = self.verify(
            self.envelope(), expected_signer_key_sha256=fingerprint
        )

        self.assertEqual("ACCEPTED", result["verdict"])
        self.assertEqual("OFFLINE_HISTORICAL_PARITY_OBSERVATION", result["acceptance_scope"])
        self.assertEqual("NOT_PERFORMED", result["live_readback"])
        self.assertIs(False, result["deployment_authorized"])
        self.assertIs(False, result["paid_inference_authorized"])
        self.assertEqual(SOURCE_SHA, result["source_revision"])
        self.assertEqual(HF_SHA, result["hf_revision"])
        self.assertEqual(fingerprint, result["signer_key_sha256"])

    def test_tampered_payload_fails_real_signature_verification(self):
        envelope = self.envelope()
        evidence = json.loads(base64.b64decode(envelope["payload"]))
        evidence["deployment"]["provider_stage"] = "PAUSED"
        payload = json.dumps(
            evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        envelope["payload"] = base64.b64encode(payload).decode("ascii")
        envelope["digest"] = hashlib.sha256(payload).hexdigest()

        self.assert_rejected("SIGNATURE_VERIFICATION_FAILED", envelope)

    def test_wrong_trusted_public_key_is_rejected(self):
        _other_private, other_public = szl_receipt.generate_keypair()
        original_public = self.public_key
        self.public_key = other_public
        try:
            self.assert_rejected("SIGNATURE_VERIFICATION_FAILED", self.envelope(private_key=self.private_key))
        finally:
            self.public_key = original_public

    def test_envelope_organ_does_not_substitute_for_expected_key_identity(self):
        self.assert_rejected(
            "SIGNER_IDENTITY_MISMATCH",
            self.envelope(organ="approved-prod-signer"),
            expected_signer_key_sha256="0" * 64,
        )

    def test_unsigned_honest_envelope_is_rejected(self):
        self.assert_rejected("UNSIGNED_EVIDENCE", self.envelope(private_key=None))

    def test_changed_payload_digest_is_rejected(self):
        envelope = self.envelope()
        envelope["digest"] = "0" * 64
        self.assert_rejected("ARTIFACT_DIGEST_MISMATCH", envelope)

    def test_source_head_mismatch_is_source_ahead(self):
        self.assert_rejected(
            "SOURCE_AHEAD", self.envelope(), expected_source_revision=OTHER_SOURCE_SHA
        )

    def test_hf_head_mismatch_is_publisher_drift(self):
        self.assert_rejected(
            "PUBLISHER_DRIFT", self.envelope(), expected_hf_revision=OTHER_HF_SHA
        )

    def test_stale_observation_is_rejected(self):
        self.assert_rejected(
            "EVIDENCE_STALE", self.envelope(),
            now=NOW + timedelta(seconds=1), max_observation_age_seconds=0.5,
        )

    def test_future_observation_is_rejected(self):
        future = accepted_evidence(NOW + timedelta(seconds=1))
        self.assert_rejected("EVIDENCE_STALE", self.resigned(future))

    def test_missing_evidence_field_is_rejected_even_with_valid_signature(self):
        evidence = accepted_evidence()
        evidence["runtime"].pop("attested_hf_revision")
        self.assert_rejected("EVIDENCE_CONTRACT_REJECTED", self.resigned(evidence))

    def test_truth_separation_and_exact_bindings_are_required(self):
        for section, field in (
            ("truth_separation", "acceptance_state"),
            ("bindings", "source_to_publication"),
            ("bindings", "publication_to_runtime"),
        ):
            with self.subTest(section=section, field=field):
                evidence = accepted_evidence()
                evidence[section][field] = "UNVERIFIED"
                expected_code = "TRUTH_SEPARATION_REJECTED" if section == "truth_separation" else "BINDING_NOT_EXACT"
                self.assert_rejected(expected_code, self.resigned(evidence))

    def test_bool_is_not_accepted_as_an_integer(self):
        evidence = accepted_evidence()
        evidence["publication"]["expected_files"] = True
        self.assert_rejected("EVIDENCE_CONTRACT_REJECTED", self.resigned(evidence))

    def test_non_string_provider_auth_is_a_typed_rejection(self):
        evidence = accepted_evidence()
        evidence["provider_auth"] = []
        self.assert_rejected("EVIDENCE_CONTRACT_REJECTED", self.resigned(evidence))

    def test_duplicate_envelope_and_payload_keys_are_rejected(self):
        envelope = self.envelope()
        duplicate_envelope = self.encoded(envelope).replace(
            b'"signed": true', b'"signed": true, "signed": true', 1
        )
        with self.assertRaises(consumer.EvidenceVerificationFailure) as caught:
            consumer.verify_signed_evidence(
                duplicate_envelope,
                self.public_key,
                expected_source_revision=SOURCE_SHA,
                expected_hf_revision=HF_SHA,
                expected_space_id=SPACE_ID,
                max_observation_age_seconds=300,
                now=NOW,
            )
        self.assertEqual("DUPLICATE_JSON_KEY", caught.exception.code)

        payload = base64.b64decode(envelope["payload"])
        duplicate_payload = payload[:-1] + b',"schema":"duplicate"}'
        envelope["payload"] = base64.b64encode(duplicate_payload).decode("ascii")
        envelope["digest"] = hashlib.sha256(duplicate_payload).hexdigest()
        self.assert_rejected("DUPLICATE_JSON_KEY", envelope)

    def test_non_running_stage_and_artifact_drift_cannot_be_resigned_into_acceptance(self):
        for path, value, code in (
            (("deployment", "provider_stage"), "PAUSED", "PROVIDER_STAGE_NOT_RUNNING"),
            (("publication", "files", 0, "publication_sha256"), "2" * 64, "ARTIFACT_DIGEST_MISMATCH"),
        ):
            with self.subTest(path=path):
                evidence = accepted_evidence()
                target = evidence
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                self.assert_rejected(code, self.resigned(evidence))

    def test_unavailable_receipt_library_fails_closed(self):
        failure = consumer.EvidenceVerificationFailure(
            "VERIFIER_UNAVAILABLE", "test-only unavailable library"
        )
        with mock.patch.object(consumer, "_receipt_library", side_effect=failure):
            self.assert_rejected("VERIFIER_UNAVAILABLE", self.envelope())

    def test_cli_reads_only_explicit_files_and_returns_json_verdict(self):
        now = datetime.now(timezone.utc)
        with TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary:
            root = Path(temporary)
            envelope_path = root / "envelope.json"
            key_path = root / "test-public.pem"
            envelope_path.write_bytes(self.encoded(self.envelope(accepted_evidence(now))))
            key_path.write_bytes(self.public_key)
            output = io.StringIO()
            with redirect_stdout(output):
                returncode = consumer.main(
                    [
                        "--envelope", str(envelope_path),
                        "--public-key", str(key_path),
                        "--expected-source-revision", SOURCE_SHA,
                        "--expected-hf-revision", HF_SHA,
                        "--expected-space-id", SPACE_ID,
                        "--max-observation-age-seconds", "300",
                    ]
                )
        result = json.loads(output.getvalue())
        self.assertEqual(0, returncode)
        self.assertEqual("ACCEPTED", result["verdict"])
        self.assertEqual("NOT_PERFORMED", result["live_readback"])


if __name__ == "__main__":
    unittest.main()
