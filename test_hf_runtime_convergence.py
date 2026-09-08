from __future__ import annotations

import copy
import unittest

from scripts import wait_hf_space_runtime as wait


REPO_ID = wait.CANONICAL_REPO_ID
PUBLICATION_SHA = "a" * 40
STALE_SHA = "b" * 40


class SequenceClient:
    def __init__(self, infos, runtimes):
        self.infos = list(infos)
        self.runtimes = list(runtimes)

    @staticmethod
    def _take(values):
        if not values:
            raise AssertionError("No provider response remains")
        if len(values) == 1:
            return copy.deepcopy(values[0])
        return copy.deepcopy(values.pop(0))

    def get_json(self, url, *, timeout):
        del timeout
        if url == wait.provider_info_url(REPO_ID):
            return self._take(self.infos)
        if url == wait.provider_runtime_url(REPO_ID):
            return self._take(self.runtimes)
        raise AssertionError(f"Unexpected URL: {url}")


class RuntimeObservationTests(unittest.TestCase):
    def test_null_stage_is_retryable_provider_transition(self):
        with self.assertRaises(wait.RuntimeWaitError) as caught:
            wait.classify_observation(
                {"private": False, "sha": PUBLICATION_SHA},
                {"stage": None, "sha": None},
            )
        self.assertEqual("RUNTIME_METADATA_PENDING", caught.exception.code)
        self.assertTrue(caught.exception.retryable)

    def test_null_runtime_revision_is_retryable_provider_transition(self):
        with self.assertRaises(wait.RuntimeWaitError) as caught:
            wait.classify_observation(
                {"private": False, "sha": PUBLICATION_SHA},
                {"stage": "BUILDING", "sha": None},
            )
        self.assertEqual("RUNTIME_METADATA_PENDING", caught.exception.code)
        self.assertTrue(caught.exception.retryable)

    def test_running_stale_revision_is_retryable(self):
        with self.assertRaises(wait.RuntimeWaitError) as caught:
            wait.classify_observation(
                {"private": False, "sha": PUBLICATION_SHA},
                {"stage": "RUNNING", "sha": STALE_SHA},
            )
        self.assertEqual("RUNTIME_REVISION_PENDING", caught.exception.code)
        self.assertTrue(caught.exception.retryable)

    def test_nonempty_malformed_revision_fails_closed(self):
        with self.assertRaises(wait.RuntimeWaitError) as caught:
            wait.classify_observation(
                {"private": False, "sha": PUBLICATION_SHA},
                {"stage": "RUNNING", "sha": "not-a-sha"},
            )
        self.assertEqual("MALFORMED_PROVIDER_RESPONSE", caught.exception.code)
        self.assertFalse(caught.exception.retryable)

    def test_terminal_provider_stage_fails_closed(self):
        with self.assertRaises(wait.RuntimeWaitError) as caught:
            wait.classify_observation(
                {"private": False, "sha": PUBLICATION_SHA},
                {"stage": "BUILD_ERROR", "sha": PUBLICATION_SHA},
            )
        self.assertEqual("PROVIDER_RUNTIME_TERMINAL_STATE", caught.exception.code)
        self.assertFalse(caught.exception.retryable)

    def test_wait_converges_across_null_building_stale_and_running_states(self):
        client = SequenceClient(
            infos=[
                {"private": False, "sha": PUBLICATION_SHA},
                {"private": False, "sha": PUBLICATION_SHA},
                {"private": False, "sha": PUBLICATION_SHA},
                {"private": False, "sha": PUBLICATION_SHA},
            ],
            runtimes=[
                {"stage": None, "sha": None},
                {"stage": "BUILDING", "sha": STALE_SHA},
                {"stage": "RUNNING", "sha": STALE_SHA},
                {"stage": "RUNNING", "sha": PUBLICATION_SHA},
            ],
        )
        clock = [0.0]

        def monotonic():
            return clock[0]

        def sleep(seconds):
            clock[0] += seconds

        receipt = wait.wait_for_runtime(
            repo_id=REPO_ID,
            client=client,
            timeout_seconds=30,
            poll_interval_seconds=1,
            request_timeout_seconds=2,
            sleep=sleep,
            monotonic=monotonic,
        )
        self.assertEqual("CONVERGED", receipt["status"])
        self.assertEqual(4, len(receipt["attempts"]))
        self.assertEqual(PUBLICATION_SHA, receipt["observation"]["runtime_revision"])
        self.assertFalse(receipt["credential_value_recorded"])

    def test_target_is_fixed_to_canonical_router_space(self):
        with self.assertRaises(wait.RuntimeWaitError) as caught:
            wait.wait_for_runtime(
                repo_id="SZLHOLDINGS/another-router",
                client=SequenceClient([], []),
                timeout_seconds=30,
                poll_interval_seconds=1,
                request_timeout_seconds=2,
            )
        self.assertEqual("TARGET_REJECTED", caught.exception.code)
        self.assertFalse(caught.exception.retryable)


if __name__ == "__main__":
    unittest.main()
