import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "server.py"
INDEX = ROOT / "index.html"
WIDGET = ROOT / "assets" / "szl_verify_widget.js"


class CanonicalDomainContractTests(unittest.TestCase):
    def test_verifier_client_has_no_legacy_api_base(self):
        server = SERVER.read_text(encoding="utf-8")
        index = INDEX.read_text(encoding="utf-8")
        widget = WIDGET.read_text(encoding="utf-8")
        self.assertNotIn("connect-src 'self' https://a11oy.net", server)
        self.assertNotIn("base: 'https://a11oy.net'", index)
        self.assertNotIn("DEFAULT_BASE = 'https://a11oy.net'", widget)

    def test_alloy_console_link_is_intentionally_preserved(self):
        index = INDEX.read_text(encoding="utf-8")
        self.assertIn('<a href="https://a11oy.net"', index)

    def test_widget_targets_live_receipt_verifier(self):
        widget = WIDGET.read_text(encoding="utf-8")
        self.assertIn("https://a-11-oy.com", widget)
        self.assertIn("/api/a11oy/v1/verify/receipt", widget)
        self.assertIn("JSON.stringify({envelope: envelope})", widget)

    def test_csp_allows_only_canonical_a11oy_origin(self):
        server = SERVER.read_text(encoding="utf-8")
        self.assertIn("connect-src 'self' https://a-11-oy.com", server)


if __name__ == "__main__":
    unittest.main()
