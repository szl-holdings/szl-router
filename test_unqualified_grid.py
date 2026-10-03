"""Offline regressions for cloud pricing/quota qualification in the legacy router."""

import os
import unittest
from unittest.mock import patch

from szl_router import core


MESSAGES = [{"role": "user", "content": "hello"}]


class UnqualifiedGridTests(unittest.TestCase):
    def setUp(self):
        # Isolate every provider key/URL and replace all transport with stubs.
        names = {name for provider in core.PROVIDERS.values()
                 for name in (provider.base_url_env, provider.key_env, provider.enable_env)
                 if name}
        names.add("SZL_RECEIPT_SINK")
        self.saved_env = {name: os.environ.get(name) for name in names}
        for name in names:
            os.environ.pop(name, None)
        self.old_post_chat = core._post_chat
        self.old_post_embeddings = core._post_embeddings
        self.old_emit_receipt = core._emit_route_receipt
        self.calls = []

        def chat_stub(provider, payload, timeout):
            self.calls.append(("chat", provider.name, payload["model"]))
            return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

        def embed_stub(provider, payload, timeout):
            self.calls.append(("embed", provider.name, payload["model"]))
            return {"data": [{"index": 0, "embedding": [0.5]}]}

        core._post_chat = chat_stub
        core._post_embeddings = embed_stub
        core._emit_route_receipt = lambda **kwargs: None
        core.embed_cache_clear()

    def tearDown(self):
        core._post_chat = self.old_post_chat
        core._post_embeddings = self.old_post_embeddings
        core._emit_route_receipt = self.old_emit_receipt
        core.embed_cache_clear()
        for name, value in self.saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_armed_cloud_override_has_unknown_cost_and_never_calls_upstream(self):
        cases = (("zhipu", "vendor/paid-model"),
                 ("siliconflow", "vendor/paid-model"),
                 ("groq", "vendor/paid-model"),
                 ("openrouter", "vendor/model:free"))
        for name, model in cases:
            with self.subTest(name=name):
                provider = core.PROVIDERS[name]
                os.environ[provider.key_env] = "synthetic-key"
                self.assertEqual(core.resolve_routes(f"{name}:{model}"), [(name, model)])
                self.assertEqual(core._tier_of(provider), "unqualified-grid")
                cost = core._cost_detail(provider, {}, model)
                self.assertIsNone(cost["amount_usd"])
                self.assertEqual(cost["tier"], "unqualified-grid")
                self.assertIn("unverified", cost["basis"])
                with self.assertRaises(core.RouterError) as raised:
                    core.chat(f"{name}:{model}", MESSAGES)
                self.assertEqual(len(raised.exception.attempts), 1)
                self.assertIn("unqualified", raised.exception.attempts[0].error)
                self.assertEqual(self.calls, [])
                os.environ.pop(provider.key_env, None)

    def test_cloud_embeddings_never_read_cache_or_call_upstream(self):
        old_get = core._embed_cache_get
        def forbidden_cache_read(key):
            self.fail("cloud cache read")
        core._embed_cache_get = forbidden_cache_read
        try:
            for name in ("zhipu", "siliconflow", "moonshot"):
                with self.subTest(name=name):
                    provider = core.PROVIDERS[name]
                    os.environ[provider.key_env] = "synthetic-key"
                    with self.assertRaises(core.RouterError) as raised:
                        core.embed(f"{name}:vendor/model", "hello")
                    self.assertEqual(len(raised.exception.attempts), 1)
                    self.assertIn("unqualified", raised.exception.attempts[0].error)
                    self.assertEqual(self.calls, [])
                    os.environ.pop(provider.key_env, None)
        finally:
            core._embed_cache_get = old_get

    def test_extra_cannot_replace_priced_model_or_messages(self):
        for extra in ({"model": "different-model"},
                      {"messages": [{"role": "user", "content": "different"}]}):
            with self.subTest(extra=extra):
                with self.assertRaisesRegex(ValueError, "cannot override"):
                    core.chat("box_gpu:original-model", MESSAGES, extra=extra)
                self.assertEqual(self.calls, [])

    def test_status_and_fabric_do_not_promote_armed_cloud_key(self):
        provider = core.PROVIDERS["zhipu"]
        os.environ[provider.key_env] = "synthetic-key"
        status = core.status()
        zhipu = next(row for row in status["providers"] if row["provider"] == "zhipu")
        self.assertTrue(zhipu["available"])  # Local key/URL presence only.
        self.assertEqual(zhipu["tier"], "unqualified-grid")
        fabric = core.fabric_status(include_harvest=False, allow_network=False)
        self.assertEqual(fabric["routes_armed"], 0)
        self.assertEqual(fabric["posture"], "red")
        self.assertEqual(fabric["ladder"]["tier_2_free_grid_faucets"]["providers"], [])
        candidates = fabric["ladder"]["unqualified_grid_candidates"]
        self.assertEqual(candidates["status"], "blocked")
        self.assertTrue(any(row["provider"] == "zhipu" and row["armed"]
                            for row in candidates["providers"]))

    def test_unqualified_cloud_does_not_hide_cooling_sovereign(self):
        box = core.PROVIDERS["box_gpu"]
        cloud = core.PROVIDERS["zhipu"]
        os.environ[box.base_url_env] = "http://fake-gpu.invalid/v1"
        os.environ[box.key_env] = "synthetic-key"
        os.environ[cloud.key_env] = "synthetic-key"
        self.assertFalse(core._warm_candidate_later(
            [("box_gpu", "model"), ("zhipu", "model")], 1))
        result = core.chat("box_gpu:model", MESSAGES)
        self.assertEqual(result["x_szl_provenance"]["tier"], "sovereign")
        self.assertEqual(result["x_szl_provenance"]["cost"]["amount_usd"], 0.0)
        self.assertEqual(self.calls, [("chat", "box_gpu", "model")])

    def test_paid_ledger_failure_remains_an_advisory_estimate(self):
        provider = core.PROVIDERS["moonshot"]
        os.environ[provider.key_env] = "synthetic-key"

        def broken_record(*args, **kwargs):
            raise OSError("synthetic ledger failure")

        with patch.object(core.spend_guard, "allow", return_value=(True, "ok")), \
             patch.object(core.spend_guard, "record", side_effect=broken_record):
            result = core.chat("moonshot:kimi-k2.5", MESSAGES)

        self.assertTrue(result["x_szl_provenance"]["cost"]["estimated"])
        self.assertEqual(self.calls, [("chat", "moonshot", "kimi-k2.5")])


if __name__ == "__main__":
    unittest.main()
