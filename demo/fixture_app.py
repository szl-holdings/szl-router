# SPDX-License-Identifier: Apache-2.0
"""Local fixture adapter. No model inference or external provider is performed."""
import json
import os

from router_control import app as router

FIXTURE_TOKEN = "offline-demo-caller"
os.environ.update({
    "SZL_ROUTER_ALLOWED_HOSTS": "unavailable.example.test,fixture.example.test",
    "SZL_ROUTER_ENABLE_EGRESS": "1",
    "SZL_ROUTER_TOKEN": FIXTURE_TOKEN,
    "OFFLINE_FIXTURE_TOKEN": "synthetic-fixture-only",
    "SZL_ROUTER_PROVIDERS_JSON": json.dumps({"providers": [
        {"id": name, "base_url": f"https://{host}/v1",
         "models": {"szl-default": "synthetic-contract-fixture"},
         "token_env": "OFFLINE_FIXTURE_TOKEN", "priority": priority,
         "sovereignty": 0, "cost_tier": 0, "classifications": ["public"]}
        for name, host, priority in [
            ("fixture-unavailable", "unavailable.example.test", 0),
            ("fixture-local", "fixture.example.test", 1),
        ]
    ]}),
})


async def fixture_completion(provider, payload):
    if provider.id == "fixture-unavailable":
        raise RuntimeError("Synthetic failover exercise")
    return {"id": "synthetic-offline-demo", "object": "chat.completion",
            "model": "synthetic-contract-fixture",
            "choices": [{"index": 0, "message": {"role": "assistant",
                "content": "SYNTHETIC FIXTURE: routing, failover and receipt wiring exercised. No model inference was performed."},
                "finish_reason": "stop"}]}, 200


# This adapter is only imported by the separate demo command; production has
# no fixture toggle and its normal transport/authentication stay in force.
router.call_provider = fixture_completion
app = router.app
