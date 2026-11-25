import unittest

from routing_cost.spend import (DEGRADED, EnforceDecision, HEALTHY, HonestHealth,
                                PolicyError, SpendLedger, SpendRule, UNKNOWN)

NOW = 10_000.0


class RuleTests(unittest.TestCase):
    def test_bad_model_rejected(self):
        with self.assertRaises(PolicyError):
            SpendRule("", 10.0)

    def test_bad_limit_rejected(self):
        for bad in (0.0, -1.0):
            with self.assertRaises(PolicyError):
                SpendRule("gpt-x", bad)


class EnforceTests(unittest.TestCase):
    def setUp(self):
        self.ledger = SpendLedger()
        self.ledger.add_rule(SpendRule("kimi", monthly_usd_limit=10.0))

    def test_no_policy_fails_closed(self):
        d = self.ledger.enforce("unknown-model", 0.01)
        self.assertFalse(d.allowed)
        self.assertIn("fail closed", d.reason)

    def test_over_budget_rejected_before_spend(self):
        d = self.ledger.enforce("kimi", 11.0)
        self.assertFalse(d.allowed)
        self.assertIn("over budget", d.reason)
        self.assertEqual(self.ledger.spent.get("kimi", 0.0), 0.0)  # ledger untouched by reject

    def test_admit_within_budget(self):
        d = self.ledger.enforce("kimi", 4.0)
        self.assertTrue(d.allowed)
        self.assertEqual(self.ledger.spent["kimi"], 4.0)
        d2 = self.ledger.enforce("kimi", 6.0)
        self.assertTrue(d2.allowed)
        d3 = self.ledger.enforce("kimi", 0.01)
        self.assertFalse(d3.allowed)  # 10.0 spent, cap reached

    def test_negative_amount_rejected(self):
        with self.assertRaises(PolicyError):
            self.ledger.enforce("kimi", -0.5)

    def test_bad_rule_type_rejected(self):
        with self.assertRaises(PolicyError):
            self.ledger.add_rule("not-a-rule")


class HealthTests(unittest.TestCase):
    def test_no_samples_unknown(self):
        self.assertEqual(HonestHealth().score(NOW), (UNKNOWN, None))

    def test_stale_only_input_is_unknown_never_green(self):
        h = HonestHealth(half_life_s=100.0, stale_after_s=300.0)
        h.observe(1.0, NOW - 1000.0)
        self.assertEqual(h.score(NOW)[0], UNKNOWN)

    def test_fresh_healthy(self):
        h = HonestHealth(half_life_s=3600.0)
        h.observe(0.95, NOW - 10.0)
        state, value = h.score(NOW)
        self.assertEqual(state, HEALTHY)
        self.assertGreater(value, 0.8)

    def test_fresh_low_score_degraded(self):
        h = HonestHealth(half_life_s=3600.0)
        h.observe(0.1, NOW - 10.0)
        state, value = h.score(NOW)
        self.assertEqual(state, DEGRADED)

    def test_decay_older_samples_contribute_less(self):
        h = HonestHealth(half_life_s=100.0)
        h.observe(0.0, NOW - 1000.0)
        h.observe(1.0, NOW - 1.0)
        state, value = h.score(NOW)
        self.assertEqual(state, HEALTHY)
        self.assertGreater(value, 0.9)  # fresh sample dominates decayed one

    def test_boundary_threshold_healthy(self):
        h = HonestHealth(half_life_s=3600.0)
        h.observe(0.8, NOW - 1.0)
        self.assertEqual(h.score(NOW)[0], HEALTHY)

    def test_out_of_range_score_rejected(self):
        h = HonestHealth()
        with self.assertRaises(PolicyError):
            h.observe(1.5, NOW)

    def test_decision_as_dict(self):
        d = EnforceDecision("m", True, "ok", 1.0, 2.0)
        self.assertEqual(d.as_dict()["model"], "m")


if __name__ == "__main__":
    unittest.main()
