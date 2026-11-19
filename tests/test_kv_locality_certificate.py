"""Adversarial, no-network cases for the nonbinding SZL KV interval certificate."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from router_control import kv_locality_certificate as mod

NOW = datetime(2026, 11, 19, 22, 0, tzinfo=timezone.utc)
SOURCE = "a" * 40
MODEL = "b" * 64
TOKENIZER = "c" * 64
PREFIX = "d" * 64


def stamp(seconds: int = 0) -> str:
    return (NOW + timedelta(seconds=seconds)).isoformat()


def sample() -> dict:
    def worker(name: str, prefill: int, decode: int, lo: int, hi: int) -> dict:
        return {
            "host": name, "observed_at_utc": stamp(-6),
            "source_sha": SOURCE, "model_sha256": MODEL,
            "tokenizer_sha256": TOKENIZER, "boot_id": "boot-" + name,
            "model_identity_verified": True, "serving_owner_exclusive": True,
            "canary_fresh": True, "rate_observed_at_utc": stamp(-6),
            "sample_requests": 40, "prefill_us_per_token": prefill,
            "decode_us_per_token": decode,
            "queue_us_lower": lo, "queue_us_upper": hi, "cache_claim": None,
        }
    return {
        "schema": mod.INPUT_SCHEMA, "observed_at_utc": stamp(-6),
        "protected_main_sha": SOURCE,
        "request": {
            "request_id": "req-001", "source_sha": SOURCE,
            "model_sha256": MODEL, "tokenizer_sha256": TOKENIZER,
            "prefix_sha256": PREFIX, "prompt_tokens": 512,
            "output_tokens": 128, "classification": "PUBLIC",
            "cache_layout": "STANDARD_FULL_ATTENTION",
        },
        "workers": [worker("gate1", 100, 40, 10000, 20000),
                    worker("cuda", 150, 50, 40000, 45000)],
    }


def claim(data: dict, host: str = "cuda", tokens: int = 400) -> dict:
    return {
        "prefix_sha256": PREFIX, "source_sha": SOURCE,
        "model_sha256": MODEL, "tokenizer_sha256": TOKENIZER,
        "boot_id": "boot-" + host, "observed_at_utc": stamp(-3),
        "claimed_prefix_tokens": tokens, "receipt_sha256": "f" * 64,
    }


def find(output: dict, host: str) -> dict:
    return next(row for row in output["worker_intervals"] if row["host"] == host)


class AdmissionBounds(unittest.TestCase):
    def test_robust_dominance_is_math_not_execution(self):
        out = mod.assess(sample(), now=NOW)
        self.assertEqual(out["state"], "ROBUST_DOMINANCE_CANDIDATE_REQUIRES_OWNER_ADMISSION")
        self.assertEqual(out["advisory_candidate_host"], "gate1")
        self.assertTrue(out["robust_dominance"])
        self.assertEqual(out["estimated_regret_upper_us"], 0)
        self.assertEqual(find(out, "gate1")["cache_free_upper_us"], 76320)
        self.assertEqual(find(out, "gate1")["optimistic_lower_us"], 66320)
        self.assertEqual(find(out, "cuda")["optimistic_lower_us"], 123200)
        for gate in ("execution_authorized", "route_changes_authorized",
                     "production_ready", "cache_transfer_assumed",
                     "source_identity_independently_attested",
                     "cache_receipt_signature_verified", "training_performed"):
            self.assertIs(out[gate], False)
        self.assertIsNone(out["slo_met"])
        self.assertEqual(len(out["structural_sha256_not_signature"]), 64)

    def test_unsigned_matching_cache_event_creates_only_conditional_lower_bound(self):
        data = sample()
        data["workers"][1]["cache_claim"] = claim(data)
        out = mod.assess(data, now=NOW)
        self.assertEqual(out["state"], "HOLD_INTERVAL_OVERLAP_NO_SAFE_PERFORMANCE_RANK")
        self.assertIsNone(out["advisory_candidate_host"])
        self.assertFalse(out["robust_dominance"])
        self.assertEqual(out["estimated_regret_upper_us"], 13120)
        cuda = find(out, "cuda")
        self.assertEqual(cuda["unsigned_conditional_hit_tokens"], 400)
        self.assertEqual(cuda["cache_claim_state"], "UNSIGNED_HYPOTHETICAL_ONLY")
        self.assertEqual(cuda["optimistic_lower_us"], 63200)
        self.assertEqual(cuda["cache_free_upper_us"], 128200)
        self.assertFalse(out["cache_receipt_signature_verified"])

    def test_invalid_cache_identity_never_earns_credit(self):
        for field, value in (
            ("prefix_sha256", "0" * 64),
            ("model_sha256", "0" * 64),
            ("source_sha", "0" * 40),
            ("tokenizer_sha256", "0" * 64),
            ("boot_id", "boot-old"),
        ):
            with self.subTest(field=field):
                data = sample()
                event = claim(data)
                event[field] = value
                data["workers"][1]["cache_claim"] = event
                out = mod.assess(data, now=NOW)
                self.assertEqual(out["state"],
                                 "ROBUST_DOMINANCE_CANDIDATE_REQUIRES_OWNER_ADMISSION")
                self.assertEqual(find(out, "cuda")["unsigned_conditional_hit_tokens"], 0)
                self.assertEqual(find(out, "cuda")["cache_claim_state"],
                                 "CACHE_IDENTITY_MISMATCH_IGNORED")

    def test_stale_cache_receipt_is_not_credited(self):
        data = sample()
        event = claim(data)
        event["observed_at_utc"] = stamp(-32)
        data["workers"][1]["cache_claim"] = event
        out = mod.assess(data, now=NOW)
        self.assertEqual(find(out, "cuda")["cache_claim_state"],
                         "STALE_CACHE_CLAIM_IGNORED")
        self.assertEqual(find(out, "cuda")["optimistic_lower_us"], 123200)

    def test_future_cache_within_skew_is_still_unsigned_and_not_a_route(self):
        data = sample()
        event = claim(data)
        event["observed_at_utc"] = stamp(5)
        data["workers"][1]["cache_claim"] = event
        result = mod.assess(data, now=NOW)
        self.assertEqual(result["state"], "HOLD_INTERVAL_OVERLAP_NO_SAFE_PERFORMANCE_RANK")
        self.assertEqual(find(result, "cuda")["unsigned_conditional_hit_tokens"], 400)
        self.assertFalse(result["cache_receipt_signature_verified"])
        self.assertEqual(find(result, "cuda")["cache_free_upper_us"], 128200)
        event["observed_at_utc"] = stamp(6)
        later = mod.assess(data, now=NOW)
        self.assertEqual(find(later, "cuda")["unsigned_conditional_hit_tokens"], 0)
        self.assertEqual(find(later, "cuda")["cache_claim_state"], "STALE_CACHE_CLAIM_IGNORED")

    def test_unsigned_cache_claim_never_flips_equal_upper_tie(self):
        data = sample()
        cuda = data["workers"][1]
        cuda["prefill_us_per_token"] = 100
        cuda["decode_us_per_token"] = 40
        cuda["queue_us_lower"] = 10000
        cuda["queue_us_upper"] = 20000
        baseline = mod.assess(data, now=NOW)
        self.assertEqual(baseline["state"], "HOLD_INTERVAL_OVERLAP_NO_SAFE_PERFORMANCE_RANK")
        self.assertEqual(baseline["minimax_reference_host_not_route"], "cuda")
        data["workers"][0]["cache_claim"] = claim(data, host="gate1")
        with_unverified_event = mod.assess(data, now=NOW)
        self.assertEqual(with_unverified_event["state"], baseline["state"])
        self.assertEqual(with_unverified_event["minimax_reference_host_not_route"], "cuda")
        self.assertIsNone(with_unverified_event["advisory_candidate_host"])
        self.assertFalse(with_unverified_event["cache_receipt_signature_verified"])

    def test_future_worker_timestamp_outside_skew_withholds_candidate(self):
        data = sample()
        data["workers"][0]["observed_at_utc"] = stamp(6)
        out = mod.assess(data, now=NOW)
        self.assertIn("STALE_WORKER_OBSERVATION", find(out, "gate1")["reasons"])
        self.assertEqual(out["state"], "SINGLE_CANDIDATE_REQUIRES_OWNER_ADMISSION")


    def test_never_credit_more_than_supplied_prompt(self):
        data = sample()
        data["workers"][1]["cache_claim"] = claim(data, tokens=513)
        with self.assertRaisesRegex(mod.Hold, "CACHE_PREFIX_TOKENS_INVALID"):
            mod.assess(data, now=NOW)

    def test_exact_queue_interval_never_double_counts_activity(self):
        data = sample()
        out = mod.assess(data, now=NOW)
        upper = (512 * 100 + 128 * 40 + 20000)
        lower = (512 * 100 + 128 * 40 + 10000)
        self.assertEqual(find(out, "gate1")["cache_free_upper_us"], upper)
        self.assertEqual(find(out, "gate1")["optimistic_lower_us"], lower)
        self.assertTrue(out["queue_bounds_include_active_work_no_double_charge"])

    def test_worker_order_and_mapping_order_do_not_change_digest(self):
        data = sample()
        a = mod.assess(data, now=NOW)
        data["workers"].reverse()
        b = mod.assess(data, now=NOW)
        self.assertEqual(a, b)

    def test_one_unqualified_host_is_not_a_robust_comparison(self):
        data = sample()
        data["workers"][1]["serving_owner_exclusive"] = False
        out = mod.assess(data, now=NOW)
        self.assertEqual(out["state"], "SINGLE_CANDIDATE_REQUIRES_OWNER_ADMISSION")
        self.assertEqual(out["advisory_candidate_host"], "gate1")
        self.assertFalse(out["robust_dominance"])
        self.assertIn("EXCLUSIVE_OWNER_UNVERIFIED", find(out, "cuda")["reasons"])
        self.assertIsNone(find(out, "cuda")["cache_free_upper_us"])

    def test_two_unqualified_workers_never_offer_a_candidate(self):
        data = sample()
        for worker in data["workers"]:
            worker["model_identity_verified"] = False
        out = mod.assess(data, now=NOW)
        self.assertEqual(out["state"], "HOLD_NO_ADVISORY_ELIGIBLE_WORKER")
        self.assertIsNone(out["advisory_candidate_host"])

    def test_worker_model_source_and_tokenizer_drift_each_withhold(self):
        for field, value, reason in [
            ("source_sha", "f" * 40, "SOURCE_DRIFT"),
            ("model_sha256", "f" * 64, "MODEL_FINGERPRINT_DRIFT"),
            ("tokenizer_sha256", "f" * 64, "TOKENIZER_FINGERPRINT_DRIFT"),
        ]:
            with self.subTest(field=field):
                data = sample()
                data["workers"][0][field] = value
                out = mod.assess(data, now=NOW)
                self.assertIn(reason, find(out, "gate1")["reasons"])

    def test_stale_rate_and_worker_samples_are_withheld(self):
        data = sample()
        data["workers"][0]["rate_observed_at_utc"] = stamp(-150)
        data["workers"][1]["observed_at_utc"] = stamp(-150)
        out = mod.assess(data, now=NOW)
        self.assertEqual(out["state"], "HOLD_NO_ADVISORY_ELIGIBLE_WORKER")
        self.assertIn("STALE_RATE_OBSERVATION", find(out, "gate1")["reasons"])
        self.assertIn("STALE_WORKER_OBSERVATION", find(out, "cuda")["reasons"])

    def test_insufficient_samples_do_not_yield_competing_ranking(self):
        data = sample()
        data["workers"][1]["sample_requests"] = 1
        out = mod.assess(data, now=NOW)
        self.assertIn("INSUFFICIENT_RATE_SAMPLES", find(out, "cuda")["reasons"])
        self.assertFalse(out["robust_dominance"])

    def test_queue_bounds_reverse_is_hold(self):
        data = sample()
        data["workers"][0]["queue_us_lower"] = 30000
        with self.assertRaisesRegex(mod.Hold, "QUEUE_BOUNDS_REVERSED"):
            mod.assess(data, now=NOW)

    def test_bad_types_not_cast_to_ints(self):
        cases = [
            ("prompt_tokens", True), ("prompt_tokens", 20.0),
            ("prompt_tokens", float("nan")), ("output_tokens", 0),
            ("output_tokens", 32769),
        ]
        for key, value in cases:
            with self.subTest(value=str(value)):
                data = sample()
                data["request"][key] = value
                with self.assertRaises(mod.Hold):
                    mod.assess(data, now=NOW)

    def test_worker_rate_and_sample_invalid_types_hold(self):
        for key, value in [
            ("prefill_us_per_token", 0), ("prefill_us_per_token", True),
            ("decode_us_per_token", 0), ("queue_us_lower", -1),
            ("queue_us_upper", float("inf")), ("sample_requests", -1),
            ("canary_fresh", 1),
        ]:
            with self.subTest(key=key, value=str(value)):
                data = sample()
                data["workers"][0][key] = value
                with self.assertRaises(mod.Hold):
                    mod.assess(data, now=NOW)

    def test_unknown_fields_and_missing_fields_hold(self):
        data = sample()
        data["workers"][0]["private_credential"] = "not-read"
        with self.assertRaisesRegex(mod.Hold, "WORKER_FIELDS"):
            mod.assess(data, now=NOW)
        data = sample()
        del data["request"]["tokenizer_sha256"]
        with self.assertRaisesRegex(mod.Hold, "REQUEST_FIELDS"):
            mod.assess(data, now=NOW)

    def test_duplicate_workers_never_count_as_replicas(self):
        data = sample()
        data["workers"][1]["host"] = "gate1"
        with self.assertRaisesRegex(mod.Hold, "DUPLICATE_OR_MISSING_WORKER"):
            mod.assess(data, now=NOW)

    def test_expired_or_future_global_snapshot_rejected(self):
        for seconds in (-121, 6):
            data = sample()
            data["observed_at_utc"] = stamp(seconds)
            with self.assertRaisesRegex(mod.Hold, "STALE_OBSERVATION"):
                mod.assess(data, now=NOW)

    def test_naive_or_invalid_timestamp_hold(self):
        for text in ("2026-11-19T22:00:00", "banana", "2026-11-19T22:00:00+99:99"):
            data = sample()
            data["workers"][0]["observed_at_utc"] = text
            with self.assertRaises(mod.Hold):
                mod.assess(data, now=NOW)

    def test_unsupported_mla_and_unknown_classification_fail_closed(self):
        data = sample()
        data["request"]["cache_layout"] = "DEEPSEEK_MLA"
        with self.assertRaisesRegex(mod.Hold, "UNSUPPORTED_KV_LAYOUT"):
            mod.assess(data, now=NOW)
        data = sample()
        data["request"]["classification"] = "RESTRICTED"
        with self.assertRaisesRegex(mod.Hold, "CLASSIFICATION_NOT_ADMITTED"):
            mod.assess(data, now=NOW)

    def test_unpinned_and_malformed_identifiers_hold(self):
        data = sample()
        data["request"]["source_sha"] = "f" * 40
        with self.assertRaisesRegex(mod.Hold, "SOURCE_NOT_PROTECTED_MAIN"):
            mod.assess(data, now=NOW)
        data = sample()
        data["request"]["request_id"] = "x@example.invalid/path"
        with self.assertRaisesRegex(mod.Hold, "REQUEST_ID_INVALID"):
            mod.assess(data, now=NOW)

    def test_cache_receipt_is_hash_only_not_a_signature(self):
        data = sample()
        data["workers"][1]["cache_claim"] = claim(data)
        out = mod.assess(data, now=NOW)
        self.assertFalse(out["cache_receipt_signature_verified"])
        self.assertFalse(out["source_identity_independently_attested"])
        self.assertTrue(out["uncertainty_credit_is_conditional_only"])
        self.assertTrue(out["estimate_not_measured_end_to_end_latency"])
        self.assertIsNone(out["slo_met"])

    def test_no_cache_event_is_zero_hit_without_false_readiness(self):
        out = mod.assess(sample(), now=NOW)
        self.assertEqual(find(out, "gate1")["cache_claim_state"], "NO_CACHE_EVENT")
        self.assertEqual(find(out, "gate1")["unsigned_conditional_hit_tokens"], 0)
        self.assertFalse(out["production_ready"])


if __name__ == "__main__":
    unittest.main()