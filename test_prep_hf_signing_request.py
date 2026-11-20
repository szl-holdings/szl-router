"""Hermetic tests for offline HF parity signing-request preparation."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import socket
import subprocess
import unittest
import urllib.request
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import szl_receipt

from scripts import prep_hf_signing_request as prep
from scripts import verify_hf_release_evidence as consumer
from test_verify_hf_release_evidence import (
    HF_SHA,
    NOW,
    OTHER_HF_SHA,
    OTHER_SOURCE_SHA,
    SOURCE_SHA,
    SPACE_ID,
    accepted_evidence,
)


def producer_bytes(evidence=None):
    return (
        json.dumps(evidence or accepted_evidence(), indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def canonical_body(request):
    return base64.b64decode(request["canonical_evidence_body_base64"], validate=True)


class SigningRequestPreparationTests(unittest.TestCase):
    def prepare(self, raw=None, **overrides):
        arguments = {
            "expected_source_revision": SOURCE_SHA,
            "expected_hf_revision": HF_SHA,
            "expected_space_id": SPACE_ID,
            "max_observation_age_seconds": 300,
            "now": NOW,
        }
        arguments.update(overrides)
        return prep.prepare_signing_request(
            producer_bytes() if raw is None else raw, **arguments
        )

    def assert_rejected(self, code, raw=None, **overrides):
        with self.assertRaises(consumer.EvidenceVerificationFailure) as caught:
            self.prepare(raw, **overrides)
        self.assertEqual(code, caught.exception.code)

    def test_pretty_producer_bytes_accepted_without_signing_or_observing_heads(self):
        raw = producer_bytes()
        request = self.prepare(raw)
        body = canonical_body(request)

        self.assertEqual("szl.hf-parity-signing-request/v1", request["schema"])
        self.assertEqual("OWNER_REVIEW_REQUIRED", request["authority"])
        self.assertIsNone(request["signature"])
        self.assertIs(False, request["signing_verified"])
        self.assertEqual(
            hashlib.sha256(raw).hexdigest(), request["source_evidence_sha256"]
        )
        self.assertEqual(len(raw), request["source_evidence_byte_length"])
        self.assertEqual(
            hashlib.sha256(body).hexdigest(), request["canonical_evidence_body_sha256"]
        )
        self.assertEqual(len(body), request["canonical_evidence_body_byte_length"])
        self.assertNotEqual(raw, body)
        self.assertEqual(raw, producer_bytes(json.loads(body)))
        self.assertEqual(SOURCE_SHA, request["expected_source_revision"])
        self.assertEqual(HF_SHA, request["expected_hf_revision"])
        self.assertEqual(SPACE_ID, request["expected_space_id"])
        self.assertEqual(
            "OPERATOR_SUPPLIED_NOT_OBSERVED_BY_THIS_TOOL",
            request["expectation_provenance"],
        )
        self.assertIn("not the request wrapper", request["instructions"])

    def test_semantically_valid_change_changes_both_exact_hashes(self):
        original = self.prepare()
        changed_evidence = accepted_evidence()
        changed_evidence["runtime"]["witness_nonce"] = "changed-test-only-witness-nonce"
        changed = self.prepare(producer_bytes(changed_evidence))

        self.assertNotEqual(
            original["source_evidence_sha256"], changed["source_evidence_sha256"]
        )
        self.assertNotEqual(
            original["canonical_evidence_body_sha256"],
            changed["canonical_evidence_body_sha256"],
        )

    def test_semantic_tamper_is_rejected(self):
        evidence = accepted_evidence()
        evidence["deployment"]["provider_stage"] = "PAUSED"
        self.assert_rejected("PROVIDER_STAGE_NOT_RUNNING", producer_bytes(evidence))

    def test_receipt_canonicalization_and_signed_envelope_match_unchanged_consumer(
        self,
    ):
        private_key, public_key = szl_receipt.generate_keypair()
        request = self.prepare()
        body = canonical_body(request)
        evidence = json.loads(body)

        envelope = szl_receipt.sign_receipt(
            szl_receipt.Receipt(request["receipt_kind"], evidence),
            private_key,
            organ="test-only-owner-review",
        )

        self.assertEqual(body, base64.b64decode(envelope["payload"], validate=True))
        self.assertEqual(request["canonical_evidence_body_sha256"], envelope["digest"])
        result = consumer.verify_signed_evidence(
            json.dumps(envelope, sort_keys=True).encode("utf-8"),
            public_key,
            expected_source_revision=SOURCE_SHA,
            expected_hf_revision=HF_SHA,
            expected_space_id=SPACE_ID,
            max_observation_age_seconds=300,
            expected_signer_key_sha256=consumer._public_key_fingerprint(public_key),
            now=NOW,
        )
        self.assertEqual("ACCEPTED", result["verdict"])
        self.assertEqual(
            request["canonical_evidence_body_sha256"], result["payload_sha256"]
        )

    def test_missing_trusted_signature_is_rejected(self):
        request = self.prepare()
        with self.assertRaises(consumer.EvidenceVerificationFailure) as caught:
            consumer.verify_signed_evidence(
                json.dumps(request).encode("utf-8"),
                szl_receipt.generate_keypair()[1],
                expected_source_revision=SOURCE_SHA,
                expected_hf_revision=HF_SHA,
                expected_space_id=SPACE_ID,
                max_observation_age_seconds=300,
                now=NOW,
            )
        self.assertEqual("ENVELOPE_CONTRACT_REJECTED", caught.exception.code)

        _private_key, public_key = szl_receipt.generate_keypair()
        unsigned = szl_receipt.sign_receipt(
            szl_receipt.Receipt(
                request["receipt_kind"], json.loads(canonical_body(request))
            ),
            None,
            organ="test-only",
        )
        with self.assertRaises(consumer.EvidenceVerificationFailure) as caught:
            consumer.verify_signed_evidence(
                json.dumps(unsigned).encode("utf-8"),
                public_key,
                expected_source_revision=SOURCE_SHA,
                expected_hf_revision=HF_SHA,
                expected_space_id=SPACE_ID,
                max_observation_age_seconds=300,
                now=NOW,
            )
        self.assertEqual("UNSIGNED_EVIDENCE", caught.exception.code)

    def test_source_and_hf_expectation_drift_are_rejected(self):
        self.assert_rejected("SOURCE_AHEAD", expected_source_revision=OTHER_SOURCE_SHA)
        self.assert_rejected("PUBLISHER_DRIFT", expected_hf_revision=OTHER_HF_SHA)

    def test_stale_and_future_evidence_are_rejected(self):
        self.assert_rejected("EVIDENCE_STALE", now=NOW + timedelta(seconds=301))
        self.assert_rejected(
            "EVIDENCE_STALE",
            producer_bytes(accepted_evidence(NOW + timedelta(seconds=1))),
        )

    def test_duplicate_json_keys_and_invalid_floats_are_rejected(self):
        duplicate = producer_bytes().replace(
            b'"verdict": "ACCEPTED"',
            b'"verdict": "ACCEPTED",\n  "verdict": "ACCEPTED"',
            1,
        )
        self.assert_rejected("DUPLICATE_JSON_KEY", duplicate)

        for token in (b"NaN", b"Infinity", b"-Infinity", b"1e999"):
            with self.subTest(token=token):
                invalid = producer_bytes().replace(
                    b'"readiness_http_status": 200',
                    b'"readiness_http_status": ' + token,
                    1,
                )
                with self.assertRaises(consumer.EvidenceVerificationFailure):
                    self.prepare(invalid)

    def test_rejected_verdict_and_wrong_source_provenance_are_rejected(self):
        for verdict in ("REJECTED", "UNSIGNED"):
            with self.subTest(verdict=verdict):
                evidence = accepted_evidence()
                evidence["verdict"] = verdict
                self.assert_rejected("EVIDENCE_NOT_ACCEPTED", producer_bytes(evidence))

        for field, value in (("repository", "untrusted/repository"), ("path", "other")):
            with self.subTest(field=field):
                evidence = accepted_evidence()
                evidence["source"][field] = value
                self.assert_rejected(
                    "EVIDENCE_CONTRACT_REJECTED", producer_bytes(evidence)
                )

    def test_file_byte_and_count_incoherence_are_rejected(self):
        cases = []
        digest_mismatch = accepted_evidence()
        digest_mismatch["publication"]["files"][0]["publication_sha256"] = "2" * 64
        cases.append(("ARTIFACT_DIGEST_MISMATCH", digest_mismatch))

        bad_count = accepted_evidence()
        bad_count["publication"]["files_checked"] = 2
        cases.append(("EVIDENCE_CONTRACT_REJECTED", bad_count))

        missing_file = accepted_evidence()
        missing_file["publication"]["missing_files"] = ["missing.txt"]
        cases.append(("EVIDENCE_CONTRACT_REJECTED", missing_file))

        for code, evidence in cases:
            with self.subTest(code=code, evidence=evidence["publication"]):
                self.assert_rejected(code, producer_bytes(evidence))

    def test_current_time_and_evidence_input_are_strict(self):
        with self.assertRaises(consumer.EvidenceVerificationFailure) as caught:
            self.prepare(now=NOW.replace(tzinfo=None))
        self.assertEqual("INVALID_VERIFICATION_INPUT", caught.exception.code)
        self.assert_rejected("MALFORMED_JSON", bytearray(producer_bytes()))
        self.assert_rejected(
            "INVALID_VERIFICATION_INPUT", max_observation_age_seconds=0
        )

    def test_cli_reads_and_writes_only_explicit_files_without_network_or_git(self):
        with TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary:
            root = Path(temporary)
            evidence_path = root / "evidence.json"
            output_path = root / "request.json"
            evidence_path.write_bytes(producer_bytes())
            arguments = [
                "--evidence",
                str(evidence_path),
                "--output",
                str(output_path),
                "--expected-source-revision",
                SOURCE_SHA,
                "--expected-hf-revision",
                HF_SHA,
                "--expected-space-id",
                SPACE_ID,
                "--max-observation-age-seconds",
                "300",
                "--current-timestamp",
                "2026-11-20T12:00:00Z",
            ]
            output = io.StringIO()
            with (
                mock.patch.object(
                    subprocess, "run", side_effect=AssertionError("git called")
                ),
                mock.patch.object(
                    urllib.request,
                    "urlopen",
                    side_effect=AssertionError("network called"),
                ),
                mock.patch.object(
                    socket,
                    "create_connection",
                    side_effect=AssertionError("network called"),
                ),
                redirect_stdout(output),
            ):
                returncode = prep.main(arguments)
            request = json.loads(output_path.read_bytes())

        self.assertEqual(0, returncode)
        self.assertEqual("PREPARED", json.loads(output.getvalue())["verdict"])
        self.assertIsNone(request["signature"])
        self.assertIs(False, request["signing_verified"])


if __name__ == "__main__":
    unittest.main()
