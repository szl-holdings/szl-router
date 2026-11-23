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
            "response": (json.dumps({"correction": "Require visible assistant output",
                                    "negative_test": "Reject blank choices"})
                         if payload["model"] == council.SEATS[0][1]
                         else "Defect: a nonempty choices list can hide a blank answer. "
                              "Negative test: choices with content='' must fail."),
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
        self.assertLessEqual(payload["options"]["num_predict"], 200)
        if payload["model"] == council.SEATS[0][1]:
            self.assertEqual(payload.get("format"), council.KIMI_OUTPUT_SCHEMA)
        else:
            self.assertNotIn("format", payload)
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

    def test_reject_reasoning_markup_in_all_seats_without_relay(self):
        cases = (
            "◁think▷HIDDEN_MARKER_9c",
            "<think>HIDDEN_MARKER_9c",
            "<thi" + chr(0x200B) + "nk>HIDDEN_MARKER_9c",
            "<thi" + chr(0xFE0F) + "nk>HIDDEN_MARKER_9c",
            "<ᵀhink>HIDDEN_MARKER_9c",
            "<thi" + chr(0x1160) + "nk>HIDDEN_MARKER_9c",
            "<thi" + chr(0x0301) + "nk>HIDDEN_MARKER_9c",
            "<think >HIDDEN_MARKER_9c",
            "＜think＞HIDDEN_MARKER_9c",
            "[think]HIDDEN_MARKER_9c",
            "◁thi" + chr(0x200B) + "nk▷HIDDEN_MARKER_9c",
        )
        for seat_name in ("Kimi", "Mistral"):
            for suspect in cases:
                with self.subTest(seat=seat_name, suspect=ascii(suspect)):
                    def respond(payload):
                        name = next(name for name, model, _, _ in council.SEATS
                                    if model == payload["model"])
                        if name == "Kimi":
                            correction = suspect if name == seat_name else "Check visible content"
                            value = json.dumps({"correction": correction,
                                                "negative_test": "Reject blank output"})
                        else:
                            value = suspect if name == seat_name else "Visible negative test."
                        return {"model": payload["model"], "response": value,
                                "done": True, "done_reason": "stop"}
                    self.response = respond
                    start = len(self.calls)
                    report = council.observe_cycle(self.fetch, observed_at="synthetic")
                    self.assertEqual(report["local_answer_count"], 3)
                    held = next(row for row in report["seats"] if row["seat"] == seat_name)
                    self.assertEqual(held["outcome"], "UNAVAILABLE")
                    self.assertNotIn("text", held)
                    past_bad_seat = False
                    for route, payload in self.calls[start:]:
                        if route != "/api/generate":
                            continue
                        name = next(n for n, model, _, _ in council.SEATS
                                    if model == payload["model"])
                        if past_bad_seat:
                            self.assertNotIn("HIDDEN_MARKER_9c", payload["prompt"])
                        if name == seat_name:
                            past_bad_seat = True
                    self.assertTrue(past_bad_seat)

    def test_plain_advice_contract_denies_markup_delimiters(self):
        for bad in ("x < y", "x > y", "array[index]", "<thing>",
                    "◁think▷", "[thinking]", "＜thing＞"):
            with self.subTest(bad=bad):
                self.assertFalse(council.safe_visible_advice(bad))
        for good in ("Plain safe advice.", "Use a negative test.",
                     '{"correction":"plain","negative_test":"check"}'):
            with self.subTest(good=good):
                self.assertTrue(council.safe_visible_advice(good))

    def test_lone_surrogate_withheld_per_seat_and_chain_survives(self):
        for seat_name in ("Kimi", "Mistral"):
            with self.subTest(seat=seat_name):
                def respond(payload):
                    name = next(name for name, model, _, _ in council.SEATS
                                if model == payload["model"])
                    if name == "Kimi":
                        value = json.dumps({
                            "correction": "\ud800" if seat_name == "Kimi" else "Visible",
                            "negative_test": "Check nonblank output"
                        })
                    else:
                        value = "\ud800" if name == seat_name else "Visible advice."
                    return {"model": payload["model"], "response": value,
                            "done": True, "done_reason": "stop"}
                self.response = respond
                start = len(self.calls)
                report = council.observe_cycle(self.fetch, observed_at="synthetic")
                self.assertEqual(report["local_answer_count"], 3)
                held = next(row for row in report["seats"] if row["seat"] == seat_name)
                self.assertEqual(held["outcome"], "UNAVAILABLE")
                self.assertNotIn("text", held)
                self.assertEqual(len(report["terminal_chain_sha256"]), 64)
                for route, payload in self.calls[start:]:
                    if route == "/api/generate":
                        self.assertNotIn("\ud800", payload["prompt"])

    def test_live_call_error_is_withheld_not_promoted(self):
        def network_refused(route, payload):
            if route == "/api/tags":
                return fixtures()
            raise urllib.error.URLError("no local Ollama")
        report = council.observe_cycle(network_refused, observed_at="T")
        self.assertEqual(report["local_answer_count"], 0)
        self.assertTrue(all("text" not in row for row in report["seats"]))

    def test_kimi_structured_completion_rejects_nonfinal_and_ambiguous_json(self):
        valid = json.dumps({"correction": "Require visible assistant output",
                            "negative_test": "Reject blank content"})
        self.assertEqual(json.loads(council.decode_kimi_advice(valid))["negative_test"],
                         "Reject blank content")
        for malformed in (
            "", "some free text", '{"correction":"yes"}',
            '{"correction":"yes","negative_test":""}',
            '{"correction":"yes","negative_test":"x","hidden":"unverified"}',
            '{"correction":"yes","correction":"x","negative_test":"test"}',
            '{"correction":"yes","negative_test":"\\u200b"}',
            '{"correction":"yes","negative_test":"x" +',
        ):
            with self.subTest(malformed=malformed):
                self.assertIsNone(council.decode_kimi_advice(malformed))

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
