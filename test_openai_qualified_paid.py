"""Offline, deterministic tests for the OpenAI owner-qualified paid route.

Hermetic: no network egress, no armed real providers, temp ledger files, and a
monkeypatched core._post_chat for the served-path tests so nothing leaves the
process. Mirrors the ci.yml honesty contract: collected by plain pytest.
"""
import json
import os
import tempfile
import unittest

from szl_router import core, spend_guard

_MSGS = [{"role": "user", "content": "ping"}]


class OpenAIProviderTests(unittest.TestCase):
    def test_provider_registered_and_tiered(self):
        p = core.PROVIDERS["openai"]
        self.assertEqual(p.key_env, "OPENAI_API_KEY")
        self.assertFalse(p.sovereign)
        self.assertEqual(p.energy_source, "grid")
        self.assertEqual(core._tier_of(p), "qualified-paid")

    def test_paid_routes_are_strictly_last_fallback(self):
        self.assertEqual(core.MODEL_ROUTES["szl-large"][-1], ("openai", "gpt-5.4"))
        self.assertEqual(core.MODEL_ROUTES["szl-fast"][-1], ("openai", "gpt-5-mini"))
        self.assertEqual(core.MODEL_ROUTES["szl-coder"][-1], ("openai", "gpt-5.4"))
        for logical in ("szl-large", "szl-fast", "szl-coder"):
            self.assertEqual(
                [n for n, _m in core.MODEL_ROUTES[logical]].count("openai"), 1)
            # Everything before the paid fallback is sovereign or grid.
            for name, _up in core.MODEL_ROUTES[logical][:-1]:
                self.assertNotEqual(name, "openai")

    def test_reviewed_rates(self):
        self.assertEqual(spend_guard.rate_for("gpt-5.5")[:2], (0.005, 0.030))
        self.assertEqual(spend_guard.rate_for("gpt-5.4")[:2], (0.0025, 0.015))
        self.assertEqual(spend_guard.rate_for("gpt-5-mini")[:2], (0.00025, 0.002))
        # Substring order: gpt-5.4-mini conservatively prices AT gpt-5.4.
        self.assertEqual(spend_guard.rate_for("gpt-5.4-mini")[:2], (0.0025, 0.015))

    def test_fabric_status_lists_qualified_paid_bucket(self):
        st = core.fabric_status(include_harvest=False)
        ladder = st["ladder"]
        self.assertIn("tier_3b_qualified_paid", ladder)
        provs = ladder["tier_3b_qualified_paid"]["providers"]
        self.assertTrue(provs)
        self.assertEqual(provs[0]["provider"], "openai")
        self.assertEqual(provs[0]["note"], core.PROVIDERS["openai"].note)


class ReserveCallTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._prev = {k: os.environ.get(k) for k in
                       ("SZL_SPEND_LEDGER_FILE", "SZL_SPEND_CAP_USD",
                        "SZL_SPEND_KILL_FILE")}
        os.environ["SZL_SPEND_LEDGER_FILE"] = os.path.join(self._td.name, "ledger.jsonl")
        os.environ["SZL_SPEND_CAP_USD"] = "1.00"

    def tearDown(self):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._td.cleanup()

    def test_no_ceiling_refuses(self):
        ok, why, _est = spend_guard.reserve_call(
            "gpt-5.4", prompt_chars=100, max_tokens=None)
        self.assertFalse(ok)
        self.assertIn("max_tokens", why)

    def test_zero_ceiling_refuses(self):
        ok, why, _est = spend_guard.reserve_call(
            "gpt-5.4", prompt_chars=100, max_tokens=0)
        self.assertFalse(ok)
        self.assertIn("max_tokens", why)

    def test_cap_breach_refuses(self):
        spend_guard.record(0.99, source="test-warm-ledger")
        ok, why, est = spend_guard.reserve_call(
            "gpt-5.4", prompt_chars=4000, max_tokens=2000)
        # prompt ~1001 tok: 1.001*0.0025 + 2*0.015 ~ 0.0325; 0.99 + 0.0325 > 1.00
        self.assertFalse(ok)
        self.assertIn("cap", why)
        self.assertGreater(est, 0)

    def test_within_cap_allows(self):
        ok, why, est = spend_guard.reserve_call(
            "gpt-5.4", prompt_chars=100, max_tokens=100)
        self.assertTrue(ok, why)
        self.assertEqual(est, round(26 * 0.0025 / 1000 + 100 * 0.015 / 1000, 6))

    def test_kill_switch_refuses(self):
        kf = os.path.join(self._td.name, "KILL")
        open(kf, "w").close()
        os.environ["SZL_SPEND_KILL_FILE"] = kf
        try:
            ok, why, _est = spend_guard.reserve_call(
                "gpt-5.4", prompt_chars=100, max_tokens=100)
            self.assertFalse(ok)
            self.assertIn("kill-switch", why)
        finally:
            os.environ.pop("SZL_SPEND_KILL_FILE", None)


class ChatDispatchTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._prev = {k: os.environ.get(k) for k in
                      ("SZL_SPEND_LEDGER_FILE", "SZL_SPEND_CAP_USD",
                       "OPENAI_API_KEY")}
        os.environ["SZL_SPEND_LEDGER_FILE"] = os.path.join(self._td.name, "ledger.jsonl")
        os.environ["SZL_SPEND_CAP_USD"] = "1.00"
        os.environ["OPENAI_API_KEY"] = "sk-test-qualified-paid-hermetic-fake-key"
        self._orig_post = core._post_chat

    def tearDown(self):
        core._post_chat = self._orig_post
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._td.cleanup()

    def test_qualified_paid_serves_under_reservation_and_ledgers(self):
        seen = {}

        def fake_post(provider, payload, timeout):
            seen["provider"] = provider.name
            seen["payload"] = payload
            return {"choices": [{"message": {"role": "assistant",
                                             "content": "pong"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

        core._post_chat = fake_post
        result = core.chat("openai:gpt-5.4", _MSGS, max_tokens=100)
        prov = result["x_szl_provenance"]
        self.assertEqual(seen["provider"], "openai")
        self.assertEqual(seen["payload"]["model"], "gpt-5.4")
        self.assertEqual(seen["payload"]["max_tokens"], 100)
        self.assertEqual(prov["served_by"], "openai:gpt-5.4")
        self.assertEqual(prov["tier"], "qualified-paid")
        self.assertFalse(prov["sovereign"])
        self.assertEqual(prov["cost"]["tier"], "qualified-paid")
        self.assertTrue(prov["cost"]["estimated"])
        self.assertAlmostEqual(prov["cost"]["amount_usd"],
                               10 * 0.0025 / 1000 + 5 * 0.015 / 1000)
        rows = [json.loads(l) for l in
                open(os.environ["SZL_SPEND_LEDGER_FILE"], encoding="utf-8")
                if l.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "qualified-paid:openai")
        self.assertEqual(rows[0]["meta"]["model"], "gpt-5.4")
        self.assertEqual(rows[0]["meta"]["served_by"], "openai:gpt-5.4")

    def test_missing_ceiling_refuses_before_transport(self):
        calls = []

        def fake_post(provider, payload, timeout):
            calls.append(payload)
            return {}

        core._post_chat = fake_post
        with self.assertRaises(core.RouterError) as raised:
            core.chat("openai:gpt-5.4", _MSGS)  # no max_tokens ceiling
        self.assertEqual(calls, [])
        self.assertIn("pricing/reservation refused",
                      raised.exception.attempts[0].error)
        self.assertIn("max_tokens", raised.exception.attempts[0].error)

    def test_cap_breach_refuses_before_transport(self):
        spend_guard.record(1.00, source="test-cap-exhaust")
        calls = []

        def fake_post(provider, payload, timeout):
            calls.append(payload)
            return {}

        core._post_chat = fake_post
        with self.assertRaises(core.RouterError) as raised:
            core.chat("openai:gpt-5.4", _MSGS, max_tokens=1000)
        self.assertEqual(calls, [])
        self.assertIn("pricing/reservation refused",
                      raised.exception.attempts[0].error)
        self.assertIn("cap", raised.exception.attempts[0].error)

    def test_logical_model_falls_through_to_paid_last(self):
        def fake_post(provider, payload, timeout):
            return {"choices": [{"message": {"role": "assistant", "content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

        core._post_chat = fake_post
        result = core.chat("szl-fast", _MSGS, max_tokens=50)
        prov = result["x_szl_provenance"]
        self.assertEqual(prov["served_by"], "openai:gpt-5-mini")
        self.assertEqual(prov["tier"], "qualified-paid")
        # Sovereign nodes were honestly tried/skipped before the paid fallback.
        self.assertIn("omen_gpu", [a["provider"] for a in prov["attempts"]])


if __name__ == "__main__":
    unittest.main()
