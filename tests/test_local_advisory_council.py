# SPDX-License-Identifier: Apache-2.0
"""No-network, synthetic only. No live model is needed in CI."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error

from scripts import local_advisory_council as council


def fixtures():
    return {
        "models": [
            {"name": model, "digest": digest}
            for _, model, digest, _ in council.SEATS
        ]
    }


class LocalCouncilTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.response = lambda payload: {
            "model": payload["model"],
            "response": "Defect: a nonempty choices list can hide a blank answer. "
                        "Negative test: choices with content='' must fail.",
            "done": True,
            "done_reason": "stop",
        }

    def fetch(self, route, payload):
        self.calls.append((route, payload))
        if route == "/api/tags":
            return fixtures()
        self.assertEqual(route, "/api/generate")
        self.assertFalse(payload["stream"])
        self.assertFalse(payload["think"])
        self.assertFalse(payload["options"]["temperature"])
        self.assertLessEqual(payload["options"]["num_predict"], 100)
        return self.response(payload)

    def test_all_four_source_bound_advisors_return_with_replayable_chain(self):
        report = council.observe_cycle(self.fetch, observed_at="2026-11-23T00:00:00Z")
        self.assertEqual(report["local_answer_count"], 4)
        self.assertEqual(len([p for r, p in self.calls if r == "/api/generate"]), 4)
        self.assertFalse(report["execution_authorized"])
        self.assertFalse(report["publication_authorized"])
        self.assertFalse(report["production_qualified"])
        self.assertTrue(all(v == "UNQUALIFIED_NOT_CALLED" for v in report["external_seats"].values()))
        digest = "0" * 64
        for row in report["seats"]:
            body = {k: v for k, v in row.items() if k != "chain_sha256"}
            digest = hashlib.sha256(bytes.fromhex(digest) + council.canonical(body)).hexdigest()
            self.assertEqual(row["chain_sha256"], digest)
        self.assertEqual(report["terminal_chain_sha256"], digest)

    def test_missing_pin_denies_one_seat_without_model_request(self):
        def missing(route, payload):
            if route == "/api/tags":
                inventory = fixtures()
                inventory["models"][0]["digest"] = "0" * 64
                return inventory
            return self.fetch(route, payload)
        report = council.observe_cycle(missing, observed_at="T")
        self.assertEqual(report["local_answer_count"], 3)
        self.assertEqual(report["seats"][0]["reason"], "PIN_NOT_OBSERVED")
        self.assertNotIn("text", report["seats"][0])

    def test_duplicate_or_malformed_inventory_is_rejected(self):
        for malformed in (
            {"models": [{"name": "duplicated", "digest": "a"*64},
                        {"name": "duplicated", "digest": "b"*64}]},
            {"models": [{"name": "bad", "digest": "latest"}]},
            {"models": "not-a-list"},
        ):
            with self.subTest(malformed=malformed):
                with self.assertRaisesRegex(ValueError, "LOCAL_INVENTORY_INVALID"):
                    council.inventory(lambda *_: malformed)

    def test_empty_zero_width_and_incomplete_are_not_successes(self):
        cases = (
            {"response": "", "done": True, "done_reason": "stop"},
            {"response": "\u200b\ufeff", "done": True, "done_reason": "stop"},
            {"response": "partial", "done": True, "done_reason": "length"},
            {"response": "partial", "done": False, "done_reason": "stop"},
            {"response": "partial", "done": True, "done_reason": "stop", "model": "different-model"},
        )
        for entry in cases:
            with self.subTest(entry=entry):
                self.response = lambda payload: {
                    "model": payload["model"], "response": "placeholder", "done": True,
                    "done_reason": "stop", **entry,
                }
                report = council.observe_cycle(self.fetch, observed_at="T")
                self.assertEqual(report["local_answer_count"], 0)
                self.assertTrue(all(row.get("reason") == "EMPTY_INCOMPLETE_OR_MODEL_MISMATCH"
                                    for row in report["seats"]))

    def test_reasoning_only_text_is_not_admitted_as_visible_advice(self):
        for text in ("◁think▷reasoning only", "<think>analysis</think>",
                     "[think]analysis[/think]", "text ◁/think▷ stray"):
            with self.subTest(text=text):
                self.response = lambda payload: {
                    "model": payload["model"],
                    "response": text if payload["model"] == council.SEATS[0][1]
                                else "Visible independent negative test.",
                    "done": True, "done_reason": "stop",
                }
                report = council.observe_cycle(self.fetch, observed_at="T")
                self.assertEqual(report["local_answer_count"], 3)
                self.assertEqual(report["seats"][0]["outcome"], "UNAVAILABLE")
                self.assertNotIn("text", report["seats"][0])
                self.assertEqual(report["seats"][1]["outcome"],
                                 "LOCAL_ADVISORY_TEXT_OBSERVED")

    def test_escaped_lone_surrogate_is_withheld_without_aborting_council(self):
        self.response = lambda payload: {
            "model": payload["model"],
            "response": "\ud800" if payload["model"] == council.SEATS[0][1]
                        else "Visible independent negative test.",
            "done": True, "done_reason": "stop",
        }
        report = council.observe_cycle(self.fetch, observed_at="T")
        self.assertEqual(report["local_answer_count"], 3)
        self.assertNotIn("text", report["seats"][0])
        self.assertTrue(report["terminal_chain_sha256"])
        self.assertEqual(report["seats"][1]["outcome"],
                         "LOCAL_ADVISORY_TEXT_OBSERVED")

    def test_live_call_error_is_withheld_not_promoted(self):
        def network_refused(route, payload):
            if route == "/api/tags":
                return fixtures()
            raise urllib.error.URLError("no local Ollama")
        report = council.observe_cycle(network_refused, observed_at="T")
        self.assertEqual(report["local_answer_count"], 0)
        self.assertTrue(all("text" not in row for row in report["seats"]))

    def test_output_is_private_create_only(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "receipt.json"
            council.save_new(path, {"private": "synthetic"})
            self.assertEqual(json.loads(path.read_text()), {"private": "synthetic"})
            with self.assertRaises(FileExistsError):
                council.save_new(path, {"private": "must not overwrite"})
            self.assertEqual(json.loads(path.read_text()), {"private": "synthetic"})

    def test_default_script_is_no_dispatch_and_routes_are_fixed(self):
        self.assertFalse(council.nonblank("\u200b\ufeff  "))
        self.assertFalse(council.nonblank(""))
        self.assertTrue(council.nonblank("valid"))
        with self.assertRaisesRegex(ValueError, "REFUSE_UNAPPROVED_ROUTE"):
            council.local_json("/api/other", None)
        self.assertIsNone(council.RefuseRedirect().redirect_request(None,None,302,
                                                                    "redirect",{}, "https://elsewhere.test"))


if __name__ == "__main__":
    unittest.main()
