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
        self.assertTrue(card["evidence_sufficient"])

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

    def test_window_is_per_card_not_global(self):
        # One card's fresh traffic must never decide another card's window:
        # q:m's only attempt is old relative to p:m but is q:m's OWN latest.
        rows = [
            att("p", "m", True, 0.10, 1000.0),
            att("q", "m", True, 0.50, 200.0),
        ]
        out = sc.scorecard(rows, window_s=200.0)
        self.assertEqual(out["cards"]["p:m"]["samples"], 1)
        self.assertEqual(out["cards"]["q:m"]["samples"], 1)

    def test_window_zero_keeps_exactly_latest_timestamp(self):
        rows = [
            att("p", "m", True, 0.10, 1000.0),
            att("p", "m", True, 0.30, 1000.0),
            att("p", "m", False, 9.99, 999.0),
        ]
        out = sc.scorecard(rows, window_s=0)
        card = out["cards"]["p:m"]
        self.assertEqual(card["samples"], 2)
        self.assertEqual(card["successes"], 2)
        self.assertAlmostEqual(card["latency_mean_s"], 0.20, places=9)

    def test_negative_window_raises(self):
        with self.assertRaises(ValueError):
            sc.scorecard([att("p", "m", True, 0.1, 1.0)], window_s=-1.0)

    def test_stale_card_survives_on_its_own_latest_attempt(self):
        # Per-card window, pinned: a card is never erased by ANOTHER card's
        # freshness (the old global-window bug), but its own stale rows are
        # still dropped, measured from the card's own latest attempt.
        rows = [
            att("p", "m", True, 0.10, 1000.0),   # fresh traffic elsewhere
            att("q", "m", True, 0.20, 900.0),     # q:m's own latest
            att("q", "m", False, 9.99, 100.0),   # q:m stale: 900-100=800 > 200
        ]
        out = sc.scorecard(rows, window_s=200.0)
        self.assertIn("q:m", out["cards"])
        self.assertEqual(out["cards"]["q:m"]["samples"], 1)
        self.assertEqual(out["cards"]["q:m"]["successes"], 1)
        self.assertEqual(out["cards"]["p:m"]["samples"], 1)

    def test_insufficient_samples_are_disclosed_not_scored(self):
        rows = [att("p", "m", True, 0.10, 1.0)]
        out = sc.scorecard(rows, min_samples=30)
        card = out["cards"]["p:m"]
        self.assertFalse(card["evidence_sufficient"])
        self.assertEqual(card["samples"], 1)
        self.assertIn("not qualification", card["note"])

    def test_nonfinite_latencies_are_excluded_not_averaged_in(self):
        # NaN/inf latency rows still count as ATTEMPTS (success/failure is
        # known) but must never poison the latency statistics.
        rows = [
            att("p", "m", True, 0.10, 1.0),
            att("p", "m", True, float("nan"), 2.0),
            att("p", "m", False, float("inf"), 3.0),
        ]
        out = sc.scorecard(rows)
        card = out["cards"]["p:m"]
        self.assertEqual(card["samples"], 3)
        self.assertEqual(card["successes"], 2)
        self.assertAlmostEqual(card["latency_mean_s"], 0.10, places=9)
        self.assertAlmostEqual(card["latency_p50_s"], 0.10, places=9)
        self.assertAlmostEqual(card["latency_p95_s"], 0.10, places=9)

    def test_key_collision_fails_closed(self):
        # ("a", "b:c") and ("a:b", "c") both compose to "a:b:c": silent
        # overwrite is data loss, so the second card must raise instead.
        rows = [
            att("a", "b:c", True, 0.10, 1.0),
            att("a:b", "c", True, 0.20, 2.0),
        ]
        with self.assertRaises(ValueError):
            sc.scorecard(rows)

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
            for banned in ("qualified", "armed", "tier", "sovereign",
                           "energy_source", "actionable"):
                self.assertNotIn(banned, card)
        self.assertIn("qualifies nothing", out["note"])

    def test_empty_input_is_honest_not_invented(self):
        out = sc.scorecard([])
        self.assertEqual(out["cards"], {})
        self.assertIn("direct statistics", out["note"])


if __name__ == "__main__":
    unittest.main()