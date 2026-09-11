from __future__ import annotations

import unittest

from scripts.hf_runtime_contract import CANONICAL_REPO_ID, ContractError, classify, reject_alternate_target

SHA = "a" * 40
STALE = "b" * 40


class RuntimeContractTests(unittest.TestCase):
    def test_null_stage_and_sha_retryable(self):
        result = classify({"private": False, "sha": SHA}, {"stage": None, "sha": None})
        self.assertEqual("METADATA_PENDING", result["state"])
        self.assertTrue(result["retryable"])

    def test_building_without_runtime_sha_retryable(self):
        result = classify({"private": False, "sha": SHA}, {"stage": "BUILDING", "sha": None})
        self.assertEqual("BUILDING", result["state"])
        self.assertTrue(result["retryable"])

    def test_starting_and_restarting_retryable(self):
        for stage in ("STARTING", "RESTARTING"):
            result = classify({"private": False, "sha": SHA}, {"stage": stage, "sha": SHA})
            self.assertTrue(result["retryable"])

    def test_running_stale_retryable(self):
        result = classify({"private": False, "sha": SHA}, {"stage": "RUNNING", "sha": STALE})
        self.assertEqual("RUNNING_STALE", result["state"])
        self.assertTrue(result["retryable"])

    def test_running_aligned(self):
        result = classify({"private": False, "sha": SHA}, {"stage": "RUNNING", "sha": SHA})
        self.assertEqual("RUNNING_ALIGNED", result["state"])

    def test_build_error_with_sha_fails_immediately(self):
        with self.assertRaises(ContractError) as caught:
            classify({"private": False, "sha": SHA}, {"stage": "BUILD_ERROR", "sha": SHA})
        self.assertEqual("BUILD_ERROR", caught.exception.code)
        self.assertFalse(caught.exception.retryable)

    def test_build_error_missing_sha_fails_immediately(self):
        with self.assertRaises(ContractError) as caught:
            classify({"private": False, "sha": SHA}, {"stage": "BUILD_ERROR", "sha": None})
        self.assertFalse(caught.exception.retryable)

    def test_malformed_sha_fails(self):
        with self.assertRaises(ContractError):
            classify({"private": False, "sha": SHA}, {"stage": "RUNNING", "sha": "not-a-sha"})

    def test_non_string_stage_fails(self):
        with self.assertRaises(ContractError):
            classify({"private": False, "sha": SHA}, {"stage": 12, "sha": SHA})

    def test_unknown_stage_fails(self):
        with self.assertRaises(ContractError):
            classify({"private": False, "sha": SHA}, {"stage": "EXPLODED", "sha": SHA})

    def test_private_space_pending(self):
        result = classify({"private": True, "sha": SHA}, {"stage": "RUNNING", "sha": SHA})
        self.assertEqual("VISIBILITY_PENDING", result["state"])

    def test_alternate_target_rejected_before_network(self):
        with self.assertRaises(ContractError) as caught:
            reject_alternate_target("SZLHOLDINGS/router-copy")
        self.assertEqual("TARGET_REJECTED", caught.exception.code)
        self.assertNotEqual(CANONICAL_REPO_ID, "SZLHOLDINGS/router-copy")


if __name__ == "__main__":
    unittest.main()
