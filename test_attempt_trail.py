"""Offline regressions for the attempt trail (inert, disclosure-only evidence).

Hermetic: pure stdlib, real temp directories, no network, no keys.
Authored by kimi-k3.1-pro (owner-directed coding brain), cleaned and
extended by the estate agent (UTF-8 corruption regression).
"""
import json
import os
import tempfile
import unittest

from szl_router import attempt_trail


class AttemptTrailTests(unittest.TestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.trail_path = os.path.join(
            self._temporary_directory.name, "trail.jsonl"
        )

    def test_record_read_roundtrip(self):
        self.trail_path = os.path.join(
            self._temporary_directory.name, "nested", "trail.jsonl"
        )
        expected = [
            {"provider": "alpha", "model": "m1", "ok": True,
             "latency_s": 0.2, "ts": 100.0},
            {"provider": "beta", "model": "m2", "ok": False,
             "latency_s": None, "ts": 101.0},
            {"provider": "alpha", "model": "m1", "ok": True,
             "latency_s": 0.3, "ts": 102.0},
        ]
        for item in expected:
            returned = attempt_trail.record(
                self.trail_path, item["provider"], item["model"],
                item["ok"], item["latency_s"], ts=item["ts"],
            )
            self.assertEqual(returned, item)

        result = attempt_trail.read(self.trail_path)
        disclosure = result["disclosure"]
        self.assertEqual(result["records"], expected)
        self.assertEqual(disclosure["lines"], 3)
        self.assertEqual(disclosure["kept"], 3)
        self.assertEqual(disclosure["corrupt"], 0)
        self.assertEqual(disclosure["schema"], "szl.router.attempt_trail/v1")

    def test_defaults_and_validation(self):
        first = attempt_trail.record(
            self.trail_path, "alpha", "m", True, 0.1
        )
        second = attempt_trail.record(
            self.trail_path, "alpha", "m", True, 0.2
        )
        self.assertIsInstance(first["ts"], float)
        self.assertIsInstance(second["ts"], float)
        self.assertLessEqual(first["ts"], second["ts"])

        for latency in (float("nan"), float("inf")):
            with self.subTest(latency=latency):
                with self.assertRaises(ValueError):
                    attempt_trail.record(
                        self.trail_path, "alpha", "m", True,
                        latency, ts=1.0,
                    )

        for provider in ("", " \t "):
            with self.subTest(provider=provider):
                with self.assertRaises(ValueError):
                    attempt_trail.record(
                        self.trail_path, provider, "m", True,
                        0.1, ts=1.0,
                    )

        with self.assertRaises(ValueError):
            attempt_trail.record(
                self.trail_path, "bad\nprovider", "m", True,
                0.1, ts=1.0,
            )

    def test_missing_file_is_not_error(self):
        result = attempt_trail.read(self.trail_path)
        self.assertEqual(result["records"], [])
        self.assertEqual(result["disclosure"]["lines"], 0)
        self.assertEqual(result["disclosure"]["kept"], 0)
        self.assertEqual(result["disclosure"]["corrupt"], 0)
        self.assertEqual(
            result["disclosure"]["note"],
            "direct records only; corrupt lines skipped and counted, "
            "never repaired",
        )

    def test_corrupt_lines_skipped_and_disclosed(self):
        first = attempt_trail.record(
            self.trail_path, "alpha", "m", True, 0.1, ts=1.0
        )
        second = attempt_trail.record(
            self.trail_path, "alpha", "m", False, None, ts=2.0
        )
        with open(self.trail_path, "a", encoding="utf-8") as handle:
            handle.write("this is not json\n")
            handle.write(json.dumps({
                "provider": "alpha", "model": "m",
                "ok": True, "latency_s": 0.2,
            }) + "\n")
        third = attempt_trail.record(
            self.trail_path, "beta", "m", True, 0.3, ts=3.0
        )

        result = attempt_trail.read(self.trail_path)
        self.assertEqual(result["records"], [first, second, third])
        self.assertEqual(result["disclosure"]["lines"], 5)
        self.assertEqual(result["disclosure"]["kept"], 3)
        self.assertEqual(result["disclosure"]["corrupt"], 2)

    def test_invalid_utf8_line_is_corrupt_not_fatal(self):
        # Regression (agent cleanup fix): a line with bytes that are not
        # valid UTF-8 must be counted as corrupt, never crash the read.
        attempt_trail.record(self.trail_path, "alpha", "m", True, 0.1, ts=1.0)
        with open(self.trail_path, "ab") as handle:
            handle.write(b'\xff\xfe{"provider":"x"}\n')
        attempt_trail.record(self.trail_path, "beta", "m", True, 0.2, ts=2.0)

        result = attempt_trail.read(self.trail_path)
        self.assertEqual(len(result["records"]), 2)
        self.assertEqual(result["disclosure"]["lines"], 3)
        self.assertEqual(result["disclosure"]["kept"], 2)
        self.assertEqual(result["disclosure"]["corrupt"], 1)
        self.assertEqual(result["records"][0]["provider"], "alpha")
        self.assertEqual(result["records"][1]["provider"], "beta")

    def test_scorecard_from_trail(self):
        attempt_trail.record(
            self.trail_path, "alpha", "m", True, 0.1, ts=1000.0
        )
        attempt_trail.record(
            self.trail_path, "alpha", "m", False, None, ts=1100.0
        )
        attempt_trail.record(
            self.trail_path, "alpha", "m", True, 0.2, ts=1200.0
        )
        attempt_trail.record(
            self.trail_path, "beta", "m", True, 0.3, ts=1200.0
        )

        result = attempt_trail.scorecard_from_trail(
            self.trail_path, window_s=150.0, min_samples=1
        )
        self.assertIn("cards", result)
        self.assertEqual(
            result["trail"], {"lines": 4, "kept": 4, "corrupt": 0}
        )
        self.assertEqual(result["window_s"], 150.0)
        self.assertEqual(result["min_samples"], 1)
        self.assertEqual(len(result["cards"]), 2)
        # alpha:m keeps its two freshest rows (window per card); beta:m one.
        self.assertEqual(
            sorted(card["samples"] for card in result["cards"].values()),
            [1, 2],
        )

    def test_scorecard_from_trail_empty(self):
        result = attempt_trail.scorecard_from_trail(self.trail_path)
        self.assertEqual(result["cards"], {})
        self.assertEqual(
            result["trail"], {"lines": 0, "kept": 0, "corrupt": 0}
        )

    def test_doctrine_never_qualifies(self):
        attempt_trail.record(
            self.trail_path, "alpha", "m", True, 0.1, ts=1.0
        )
        result = attempt_trail.scorecard_from_trail(
            self.trail_path, min_samples=1
        )
        self.assertIn("qualifies nothing", result["note"])
        self.assertTrue(result["cards"])
        for card in result["cards"].values():
            for key in ("qualified", "armed", "actionable"):
                self.assertNotIn(key, card)

    def test_corrupt_trail_never_invents_records(self):
        with open(self.trail_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "provider": "alpha", "model": "m", "ok": True,
                "latency_s": 0.1, "ts": "abc",
            }) + "\n")

        trail = attempt_trail.read(self.trail_path)
        self.assertEqual(trail["records"], [])
        self.assertEqual(trail["disclosure"]["corrupt"], 1)

        result = attempt_trail.scorecard_from_trail(self.trail_path)
        self.assertEqual(result["cards"], {})
        self.assertEqual(
            result["trail"], {"lines": 1, "kept": 0, "corrupt": 1}
        )


if __name__ == "__main__":
    unittest.main()