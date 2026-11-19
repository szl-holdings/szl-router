"""No-network negative controls for separate Salad/Massed capacity admission."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from router_control import coexistence_admission as mod
from scripts.assess_salad_massed import write_exclusive

NOW = datetime(2026, 11, 18, 1, 0, tzinfo=timezone.utc)
FINGERPRINT = "a" * 64
SOURCE = "b" * 40


def sample():
    return {
        "schema": mod.INPUT_SCHEMA,
        "observed_at_utc": (NOW - timedelta(seconds=8)).isoformat(),
        "protected_main_sha": SOURCE,
        "request": {
            "kind": "inference",
            "classification": "PUBLIC",
            "model_alias": "salad-a11oy-mini-r1",
            "required_artifact_fingerprint": FINGERPRINT,
            "kv_shape": {
                "layers": 32,
                "kv_heads": 8,
                "head_dim": 128,
                "dtype_bytes": 2,
                "block_tokens": 16,
                "context_tokens": 256,
                "concurrent_sequences": 1,
                "workspace_mib": 128,
            },
        },
        "massed_hosts": [
            {
                "id": name,
                "observed_at_utc": (NOW - timedelta(seconds=5)).isoformat(),
                "ready_http": 200,
                "ready": True,
                "disk_free_bytes": disk,
                "vram_free_mib": mem,
                "boot_id": "synthetic-boot-" + name,
                "catalog_boot_id": "synthetic-boot-" + name,
                "artifact_fingerprint": FINGERPRINT,
                "model_loaded_identity_verified": True,
                "canary_fresh": True,
                "coincident_training_processes": False,
                "serving_owner_exclusive": True,
            }
            for name, disk, mem in (
                ("gate1", 90 * 1024**3, 13321),
                ("cuda", 54_284_816_384, 5287),
            )
        ],
        "salad": {
            "observed_at_utc": (NOW - timedelta(seconds=5)).isoformat(),
            "project": "default",
            "groups_returned": 139,
            "groups_captured": 120,
            "other_projects_observed": False,
            "provider_access_denied": False,
            "active_replica_count": 18,
            "training_process_liveness_verified": False,
            "new_spend_authorized": False,
        },
        "gateway": {
            "cloudflare_access_deny_verified": False,
            "origin_bypass_closed_verified": False,
            "router_source_revision_verified": False,
            "hub_runtime_source_verified": False,
            "a11oy_router_configuration_present": False,
            "inference_origin_authenticated": False,
        },
    }


class ArithmeticTests(unittest.TestCase):
    def test_kv_cache_block_math_exact_with_explicit_overhead(self):
        shape = sample()["request"]["kv_shape"]
        result = mod.kv_bytes(shape)
        raw = 2 * 32 * 8 * 128 * 2 * 256
        self.assertEqual(result["raw_kv_bytes"], raw)
        self.assertEqual(result["padded_tokens_across_sequences"], 256)
        self.assertEqual(
            result["planned_reserved_bytes"],
            (raw * 11 + 9) // 10 + 128 * 1024**2,
        )
        self.assertEqual(
            result["planned_mib_ceil"],
            (result["planned_reserved_bytes"] + 2**20 - 1) // 2**20,
        )

    def test_padding_blocks_and_multi_request_memory(self):
        a = sample()["request"]["kv_shape"]
        a.update({"context_tokens": 257, "concurrent_sequences": 2})
        x = mod.kv_bytes(a)
        self.assertEqual(x["padded_tokens_across_sequences"], 544)
        self.assertGreater(
            x["planned_reserved_bytes"],
            mod.kv_bytes(sample()["request"]["kv_shape"])["planned_reserved_bytes"],
        )

    def test_no_nonfinite_or_oversized_kv_shape_fields(self):
        for value in (0, -1, 2.0, float("nan"), "8", True, 1000000):
            with self.subTest(value=value):
                a = sample()["request"]["kv_shape"]
                a["layers"] = value
                with self.assertRaises(mod.Hold):
                    mod.kv_bytes(a)

    def test_missing_kv_geometry_is_held_not_assumed_zero(self):
        a = sample()["request"]["kv_shape"]
        a.pop("kv_heads")
        with self.assertRaisesRegex(mod.Hold, "KV_SHAPE_FIELDS_REQUIRED"):
            mod.kv_bytes(a)


class CoexistenceTests(unittest.TestCase):
    def test_realistic_synthetic_capacity_does_not_wire_cloud(self):
        result = mod.assess(sample(), now=NOW)
        self.assertEqual(
            result["state"], "HOLD_AUTHENTICATED_GATEWAY_AND_SOURCE_BINDING"
        )
        self.assertEqual(result["advisory_candidate_host"], "gate1")
        self.assertFalse(result["execution_authorized"])
        self.assertFalse(result["route_changes_authorized"])
        self.assertEqual(result["salad"]["omitted_groups"], 19)
        self.assertEqual(result["salad"]["new_replicas_authorized"], 0)
        self.assertFalse(result["salad"]["full_capacity_inventory"])
        self.assertEqual(
            result["latency_rank"],
            "UNAVAILABLE_INSUFFICIENT_FRESH_MULTI_REQUEST_SAMPLE",
        )

    def test_all_gateway_fields_are_true_still_not_deployment_authority(self):
        a = sample()
        for key in a["gateway"]:
            a["gateway"][key] = True
        result = mod.assess(a, now=NOW)
        self.assertEqual(
            result["state"], "CANDIDATE_REQUIRES_SEPARATE_OWNER_RELEASE_ADMISSION"
        )
        self.assertFalse(result["execution_authorized"])
        self.assertFalse(result["paid_compute_authorized"])
        self.assertFalse(result["cloudflare_settlement_qualified"])

    def test_khipu_ollama_tag_reaches_existing_gateway_hold(self):
        # Red on main: the advisory rejects the already valid router public
        # alias khipu:latest before it can classify existing security gates.
        a = sample()
        a["request"]["model_alias"] = "khipu:latest"
        result = mod.assess(a, now=NOW)
        self.assertEqual(result["model_alias"], "khipu:latest")
        self.assertEqual(
            result["state"], "HOLD_AUTHENTICATED_GATEWAY_AND_SOURCE_BINDING"
        )
        self.assertEqual(result["advisory_candidate_host"], "gate1")
        self.assertFalse(result["execution_authorized"])
        self.assertFalse(result["route_changes_authorized"])
        self.assertFalse(result["salad_scale_out_authorized"])

    def test_tagged_alias_does_not_skip_hardware_or_owner_checks(self):
        a = sample()
        a["request"]["model_alias"] = "khipu:latest"
        for name in a["gateway"]:
            a["gateway"][name] = True
        for host in a["massed_hosts"]:
            host["model_loaded_identity_verified"] = False
        result = mod.assess(a, now=NOW)
        self.assertEqual(result["state"], "HOLD_MASSED_RESOURCE_OR_IDENTITY")
        self.assertIsNone(result["advisory_candidate_host"])
        self.assertFalse(result["execution_authorized"])
        self.assertFalse(result["paid_compute_authorized"])
        self.assertFalse(result["model_quality_qualified"])

    def test_tagged_model_names_preserve_bounded_alias_validation(self):
        for value in (
            "", ":latest", "khipu:latest/../../tokens", "https://provider",
            "khipu:latest?x=1", "khipu:latest" + chr(10) + "TOKEN", " khipu:latest",
            "khipu:latest ", "khipu:" + "x" * 100, None, 17, True,
        ):
            with self.subTest(value=value):
                a = sample()
                a["request"]["model_alias"] = value
                with self.assertRaisesRegex(mod.Hold, "MODEL_ALIAS_INVALID"):
                    mod.assess(a, now=NOW)

    def test_training_or_evaluation_cannot_touch_massed_or_add_salad_jobs(self):
        for kind in ("training", "evaluation"):
            with self.subTest(kind=kind):
                a = sample()
                a["request"]["kind"] = kind
                result = mod.assess(a, now=NOW)
                self.assertIsNone(result["advisory_candidate_host"])
                self.assertEqual(
                    result["state"], "HOLD_NO_NEW_SALAD_JOB_OR_BUDGET_AUTHORITY"
                )
                self.assertEqual(result["salad"]["new_replicas_authorized"], 0)

    def test_no_restricted_or_ambiguous_classification_egress(self):
        for label in ("RESTRICTED", "CONFIDENTIAL", "UNKNOWN", "", None, "public"):
            with self.subTest(label=label):
                a = sample()
                a["request"]["classification"] = label
                with self.assertRaisesRegex(mod.Hold, "DATA_CLASSIFICATION_DENIED"):
                    mod.assess(a, now=NOW)

    def test_fingerprint_mismatch_refuses_only_that_host(self):
        a = sample()
        a["massed_hosts"][0]["artifact_fingerprint"] = "f" * 64
        result = mod.assess(a, now=NOW)
        self.assertFalse(result["massed"][0]["advisory_eligible"])
        self.assertEqual(result["advisory_candidate_host"], "cuda")
        self.assertIn(
            "MODEL_ARTIFACT_FINGERPRINT_MISMATCH", result["massed"][0]["reasons"]
        )

    def test_stale_host_refused_even_if_rest_of_host_ready(self):
        a = sample()
        a["massed_hosts"][0]["observed_at_utc"] = (
            NOW - timedelta(seconds=121)
        ).isoformat()
        result = mod.assess(a, now=NOW)
        self.assertFalse(result["massed"][0]["advisory_eligible"])
        self.assertEqual(result["advisory_candidate_host"], "cuda")
        self.assertIn("STALE_HOST_OBSERVATION", result["massed"][0]["reasons"])

    def test_stale_snapshot_holds_entire_advisor(self):
        a = sample()
        a["observed_at_utc"] = (NOW - timedelta(seconds=121)).isoformat()
        with self.assertRaises(mod.Hold):
            mod.assess(a, now=NOW)

    def test_denied_gateway_or_unknown_process_cannot_admit_cuda(self):
        a = sample()
        a["massed_hosts"][0]["ready_http"] = 503
        a["massed_hosts"][1]["coincident_training_processes"] = None
        result = mod.assess(a, now=NOW)
        self.assertIsNone(result["advisory_candidate_host"])
        self.assertEqual(result["state"], "HOLD_MASSED_RESOURCE_OR_IDENTITY")
        self.assertIn(
            "TRAINER_RESOURCE_COLLISION_NOT_EXCLUDED", result["massed"][1]["reasons"]
        )

    def test_cuda_disk_below_reserve_blocks_even_when_loaded(self):
        a = sample()
        a["massed_hosts"][1]["disk_free_bytes"] = 50 * 1024**3 - 1
        a["massed_hosts"][0]["ready"] = False
        result = mod.assess(a, now=NOW)
        self.assertEqual(result["state"], "HOLD_MASSED_RESOURCE_OR_IDENTITY")
        self.assertIn("DISK_BELOW_HARD_RESERVE", result["massed"][1]["reasons"])

    def test_gpu_headroom_does_not_borrow_models_or_shift_trainer(self):
        a = sample()
        for host in a["massed_hosts"]:
            host["vram_free_mib"] = mod.GPU_VRAM_RESERVE_MIB[host["id"]]
        result = mod.assess(a, now=NOW)
        self.assertIsNone(result["advisory_candidate_host"])
        self.assertTrue(
            all(
                "VRAM_AFTER_KV_BELOW_RESERVE" in row["reasons"]
                for row in result["massed"]
            )
        )
        self.assertFalse(result["cross_provider_kv_transfer_assumed"])

    def test_identity_stale_boot_or_canary_blocks(self):
        for key, bad in (
            ("catalog_boot_id", "other"),
            ("model_loaded_identity_verified", None),
            ("canary_fresh", False),
            ("serving_owner_exclusive", None),
        ):
            with self.subTest(key=key):
                a = sample()
                a["massed_hosts"][0][key] = bad
                result = mod.assess(a, now=NOW)
                self.assertEqual(result["advisory_candidate_host"], "cuda")

    def test_incomplete_salad_census_never_suppressed(self):
        a = sample()
        a["salad"]["groups_captured"] = 139
        a["salad"]["other_projects_observed"] = True
        a["salad"]["training_process_liveness_verified"] = True
        result = mod.assess(a, now=NOW)
        self.assertTrue(result["salad"]["full_capacity_inventory"])
        self.assertEqual(result["salad"]["omitted_groups"], 0)
        self.assertEqual(result["salad"]["new_replicas_authorized"], 0)

    def test_salad_provider_denial_does_not_upgrade_to_empty(self):
        a = sample()
        a["salad"]["provider_access_denied"] = True
        result = mod.assess(a, now=NOW)
        self.assertFalse(result["salad"]["full_capacity_inventory"])
        self.assertIn("SALAD_PROVIDER_DENIAL_OR_UNKNOWN", result["salad"]["reasons"])

    def test_malformed_request_and_untrusted_extra_values_fail_closed(self):
        for target, key, value in (
            ("root", "token", "secret"),
            ("salad", "auth", "test"),
            ("gateway", "password", "test"),
        ):
            with self.subTest(target=target):
                a = sample()
                (a if target == "root" else a[target])[key] = value
                with self.assertRaises(mod.Hold):
                    mod.assess(a, now=NOW)

    def test_no_duplicate_or_unknown_mass_pool_nodes(self):
        a = sample()
        a["massed_hosts"][1]["id"] = "gate1"
        with self.assertRaisesRegex(mod.Hold, "DUPLICATE_OR_MISSING"):
            mod.assess(a, now=NOW)

    def test_no_automatic_goodput_from_single_canary(self):
        result = mod.assess(sample(), now=NOW)
        self.assertFalse(result["research_algorithms_implemented"])
        self.assertIsNone(result["massed"][0]["identity_digest_truncated"])


class InputAndCLIContracts(unittest.TestCase):
    def test_duplicate_json_key_refused(self):
        with self.assertRaisesRegex(mod.Hold, "DUPLICATE_JSON_KEY"):
            mod.input_json(b'{"schema":"A","schema":"B"}')

    def test_nonfinite_or_too_big_json_refused(self):
        with self.assertRaises(mod.Hold):
            mod.input_json(b'{"q":NaN}')
        with self.assertRaises(mod.Hold):
            mod.input_json(b" " * 64001)

    def test_non_string_or_naive_clock_refused(self):
        with self.assertRaises(mod.Hold):
            mod.assess(sample(), now=datetime(2026, 11, 18, 1, 0))
        a = sample()
        a["observed_at_utc"] = "2026-11-18T00:59:30"
        with self.assertRaises(mod.Hold):
            mod.assess(a, now=NOW)

    def test_exact_private_output_never_overwrites(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "record.json"
            result = mod.assess(sample(), now=NOW)
            digest = write_exclusive(path, result)
            self.assertEqual(len(digest), 64)
            body = json.loads(path.read_bytes())
            self.assertEqual(body["schema"], mod.SCHEMA)
            self.assertFalse(body["execution_authorized"])
            with self.assertRaises(FileExistsError):
                write_exclusive(path, result)


if __name__ == "__main__":
    unittest.main()
