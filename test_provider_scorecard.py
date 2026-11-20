"""Offline regressions for the provider scorecard (disclosure-only estimates).

Hermetic: pure stdlib, no network, no keys, no environment.
"""
import unittest

from szl_router import provider_scorecard as sc


def att(provider, model, ok, lat, ts):
    return (provider, model, ok, lat, ts)


class ScorecardStatsTests(unittest.TestCase):
    def test_hand_computed_card(self):
        rows = [
            att("p", "m", True, 0.10, 100.0),
            att("p", "m", True, 0.20, 101.0),
            att("p", "m", False, 5.00, 102.0),
        ]
        out = sc.scorecard(rows, window_s=None, min_samples=2)
        card = out["cards"]["p:m"]
        self.assertEqual(card["samples"], 3)
        self.assertEqual(card["successes"], 2)
        self.assertEqual(card["failures"], 1)
        self.assertAlmostEqual(card["success_rate"], 2.0 / 3.0, places=9)
        # Fail-closed: the Wilson 95% lower bound sits BELOW the raw rate.
        self.assertLess(card["success_rate_wilson95_lower"], card["success_rate"])
        self.assertGreater(card["success_rate_wilson95_lower"], 0.0)
        self.assertAlmostEqual(card["latency_mean_s"], (0.10 + 0.20 + 5.00) / 3.0,
                                places=9)
        self.assertAlmostEqual(card["latency_p50_s"], 0.20, places=9)
        self.assertAlmostEqual(card["latency_p95_s"], 5.00, places=9)
        self.assertTrue(card["actionable"])

    def test_window_filters_stale_evidence(self):
        rows = [
            att("p", "m", True, 0.10, 0.0),    # stale
            att("p", "m", False, 9.99, 900.0),  # fresh
            att("p", "m", True, 0.10, 1000.0),  # fresh
        ]
        out = sc.scorecard(rows, window_s=200.0)
        card = out["cards"]["p:m"]
        self.assertEqual(card["samples"], 2)
        self.assertEqual(card["successes"], 1)
        self.assertAlmostEqual(card["latency_p95_s"], 9.99, places=9)

    def test_insufficient_samples_are_disclosed_not_scored(self):
        rows = [att("p", "m", True, 0.10, 1.0)]
        out = sc.scorecard(rows, min_samples=30)
        card = out["cards"]["p:m"]
        self.assertFalse(card["actionable"])
        self.assertEqual(card["samples"], 1)
        self.assertIn("not qualification", card["note"])

    def test_wilson_bound_edge_cases(self):
        self.assertEqual(sc.wilson_lower(0, 0), 0.0)      # no evidence = no score
        self.assertEqual(sc.wilson_lower(1, 0), 0.0)     # incoherent input
        self.assertLess(sc.wilson_lower(1, 1), sc.wilson_lower(10, 10))  # sample size matters
        self.assertAlmostEqual(sc.wilson_lower(100, 100), 0.963, places=3)


class ScorecardDoctrineTests(unittest.TestCase):
    def test_structure_never_grows_qualification_or_arming(self):
        rows = [att("p", "m%d" % i, True, 0.1, float(i)) for i in range(5)]
        out = sc.scorecard(rows)
        for key, card in out["cards"].items():
            self.assertEqual(set(card), sc._CARD_KEYS)
            for banned in ("qualified", "armed", "tier", "sovereign", "energy_source"):
                self.assertNotIn(banned, card)
        self.assertIn("qualifies nothing", out["note"])

    def test_empty_input_is_honest_not_invented(self):
        out = sc.scorecard([])
        self.assertEqual(out["cards"], {})
        self.assertIn("direct statistics", out["note"])


if __name__ == "__main__":
    unittest.main()