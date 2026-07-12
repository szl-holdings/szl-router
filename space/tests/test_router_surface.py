import functools
import json
import re
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import HardenedHandler  # noqa: E402


CONTRACTS = {
    name: ROOT / "assets" / f"snapshot-router-{name}.json"
    for name in ("health", "models", "provenance")
}
FORBIDDEN_TOPOLOGY_TERMS = (
    "betterwithage",
    "box_gpu",
    "omen_gpu",
    "nvidia_gpu",
    "nvidia_nim",
    "tailscale",
    "tailnet",
    "ollama",
    "groq",
    "siliconflow",
    "moonshot",
    "zhipu",
)


def walk(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)
    elif isinstance(value, str):
        yield value


def walk_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_keys(child)


def load_contract(name):
    return json.loads(CONTRACTS[name].read_text(encoding="utf-8"))


class RouterSurfaceTests(unittest.TestCase):
    def test_snapshot_contracts_exist_and_parse(self):
        for name, path in CONTRACTS.items():
            self.assertTrue(path.exists(), name)
            self.assertIsInstance(load_contract(name), dict)

    def test_public_contracts_do_not_leak_private_topology(self):
        combined = "\n".join(
            str(item).lower()
            for name in CONTRACTS
            for item in walk(load_contract(name))
        )
        self.assertNotRegex(combined, r"https?://")
        self.assertNotRegex(combined, r"\b(?:10|127|169\.254|192\.168)\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
        self.assertNotRegex(combined, r"\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b")
        self.assertNotRegex(combined, r"\b(?:localhost|[^\s]+\.local)\b")
        for forbidden in FORBIDDEN_TOPOLOGY_TERMS:
            self.assertNotIn(forbidden, combined)
        keys = {
            key.lower()
            for name in CONTRACTS
            for key in walk_keys(load_contract(name))
        }
        for field in ("base_url", "endpoint", "hostname", "host", "ip_address"):
            self.assertNotIn(field, keys)

    def test_provider_identity_is_opaque_and_bounded(self):
        provenance = load_contract("provenance")
        for provider in provenance["providers"]:
            self.assertRegex(provider["provider_id"], r"^provider-(sovereign|free|paid)-\d{2}$")
            self.assertIn(provider["provider_class"], {"self_hosted_inference", "hosted_inference"})
            self.assertNotIn("provider", provider)
            self.assertNotIn("note", provider)

    def test_configuration_is_not_reported_as_availability(self):
        health = load_contract("health")
        models = load_contract("models")
        provenance = load_contract("provenance")
        self.assertNotIn("ok", health)
        self.assertEqual(health["router_runtime"], "NOT_MEASURED")
        self.assertEqual(health["provider_reachability"], "NOT_MEASURED")

        combined_keys = [
            item.lower()
            for contract in (health, models, provenance)
            for item in walk(contract)
            if isinstance(item, str)
        ]
        self.assertNotIn("available", combined_keys)

        for item in [*models["data"], *provenance["providers"]]:
            self.assertIsInstance(item["configured"], bool)
            self.assertEqual(item["live_reachable"], "NOT_MEASURED")

        for provider in provenance["providers"]:
            if provider["live_reachable"] is True:
                self.assertTrue(provider.get("last_probe_at"))
                self.assertTrue(provider.get("probe_receipt_id"))

    def test_ui_never_turns_snapshot_transport_into_live_status(self):
        app = (ROOT / "assets" / "app.js").read_text(encoding="utf-8")
        page = (ROOT / "index.html").read_text(encoding="utf-8").lower()
        self.assertIn("REACHABLE · SNAPSHOT", app)
        self.assertIn("live_reachable", app)
        self.assertNotIn("p.available", app)
        self.assertNotIn("LIVE · szl-router", app)
        for forbidden in FORBIDDEN_TOPOLOGY_TERMS:
            self.assertNotIn(forbidden, page)

    def test_http_contract_headers_are_explicit_and_uncacheable(self):
        handler = functools.partial(HardenedHandler, directory=str(ROOT))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for name in CONTRACTS:
                url = f"http://127.0.0.1:{server.server_port}/api/a11oy/v1/router/{name}"
                with urlopen(url, timeout=3) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    self.assertEqual(response.headers["X-SZL-Transport-State"], "REACHABLE")
                    self.assertEqual(response.headers["X-SZL-Evidence-State"], "SNAPSHOT")
                    json.loads(response.read())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
