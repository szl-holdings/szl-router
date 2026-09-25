from __future__ import annotations

import functools
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
sys.path.insert(0, str(ROOT))

from paint import (  # noqa: E402
    CYCLE_PATH,
    CYCLE_SCHEMA,
    ORGAN_CYCLE_URL,
    allow_chrome,
    load_published_cycle,
    paint_from_cycle,
    paint_label,
    paint_tone,
)
import server  # noqa: E402


def allowing_cycle(**overrides):
    cycle = {
        "schema": CYCLE_SCHEMA,
        "verdict": "ALLOW",
        "invariantsOk": True,
        "productionPromotion": False,
        "authority": "PROPOSAL_ONLY",
        "lambda": "CONJECTURE_1",
        "lambdaNeverATheorem": True,
        "shadow": None,
        "arithmeticScore": 0.91,
    }
    cycle.update(overrides)
    return cycle


class FakeResponse:
    def __init__(self, payload, status=200, raw=None):
        self.status = status
        if raw is not None:
            self._payload = raw
        elif isinstance(payload, bytes):
            self._payload = payload
        else:
            self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class PaintBindTests(unittest.TestCase):
    def test_null_and_invalid_cycle_cannot_paint_allow(self):
        self.assertEqual("UNAVAILABLE", paint_from_cycle(None))
        self.assertEqual("UNAVAILABLE", paint_from_cycle({"verdict": "ALLOW"}))
        self.assertEqual(
            "UNAVAILABLE",
            paint_from_cycle(allowing_cycle(schema="not-a-cycle")),
        )
        self.assertFalse(allow_chrome("UNAVAILABLE"))
        self.assertEqual("pending", paint_tone("UNAVAILABLE"))
        self.assertEqual("UNAVAILABLE", paint_label("UNAVAILABLE"))

    def test_allow_chrome_requires_bound_cycle_receipt(self):
        paint = paint_from_cycle(allowing_cycle())
        self.assertEqual("ALLOW", paint)
        self.assertTrue(allow_chrome(paint))
        self.assertEqual("allow", paint_tone(paint))
        self.assertEqual("ALLOW", paint_label(paint))

    def test_deny_verdicts_cannot_paint_allow(self):
        for verdict in ("HARD_DENY", "LAMBDA_VETO", "DENY_DEFAULT", "ESCALATE"):
            paint = paint_from_cycle(allowing_cycle(verdict=verdict))
            self.assertEqual("DENY", paint, verdict)
            self.assertFalse(allow_chrome(paint), verdict)
            self.assertEqual(verdict, paint_label(paint, verdict))

    def test_theorem_claim_and_promotion_cannot_paint_allow(self):
        # Inv2 window: Lambda is Conjecture 1, never a theorem.
        theorem_claim = allowing_cycle()
        theorem_claim["lambda"] = "THEOREM"
        theorem_claim["lambdaNeverATheorem"] = False  # never a theorem
        self.assertEqual("DENY", paint_from_cycle(theorem_claim))
        missing_posture = allowing_cycle()
        missing_posture["lambdaNeverATheorem"] = False  # never a theorem
        self.assertEqual("DENY", paint_from_cycle(missing_posture))
        self.assertEqual(
            "DENY",
            paint_from_cycle(allowing_cycle(productionPromotion=True)),
        )
        self.assertEqual(
            "DENY",
            paint_from_cycle(allowing_cycle(authority="EXECUTE")),
        )
        self.assertEqual(
            "DENY",
            paint_from_cycle(allowing_cycle(invariantsOk=False)),
        )
        self.assertEqual(
            "DENY",
            paint_from_cycle(allowing_cycle(invariantsOk=None)),
        )

    def test_executable_shadow_is_hard_paint_deny(self):
        self.assertEqual(
            "DENY",
            paint_from_cycle(allowing_cycle(shadow={"executable": True})),
        )
        self.assertEqual(
            "ALLOW",
            paint_from_cycle(allowing_cycle(shadow={"executable": False})),
        )

    def test_arithmetic_would_allow_is_a_ghost(self):
        paint = paint_from_cycle(
            allowing_cycle(verdict="LAMBDA_VETO", arithmeticScore=0.99, divergent=True)
        )
        self.assertEqual("DENY", paint)
        self.assertFalse(allow_chrome(paint))

    def test_load_published_cycle_is_fail_closed(self):
        self.assertEqual(
            allowing_cycle(),
            load_published_cycle(urlopen=lambda request, timeout=None: FakeResponse(allowing_cycle())),
        )
        self.assertIsNone(
            load_published_cycle(urlopen=lambda request, timeout=None: FakeResponse({"schema": "nope"}))
        )
        self.assertIsNone(
            load_published_cycle(urlopen=lambda request, timeout=None: FakeResponse(allowing_cycle(), status=503))
        )
        self.assertIsNone(
            load_published_cycle(urlopen=lambda request, timeout=None: FakeResponse(None, raw=b"not-json"))
        )

        def boom(request, timeout=None):
            raise TimeoutError("organ unreachable")

        self.assertIsNone(load_published_cycle(urlopen=boom))

    def test_js_helper_mirrors_python_contract(self):
        js = (ROOT / "assets" / "paint.js").read_text(encoding="utf-8")
        self.assertIn(f"CYCLE_SCHEMA = '{CYCLE_SCHEMA}'", js)
        self.assertIn(f"ORGAN_CYCLE_URL = '{ORGAN_CYCLE_URL}'", js)
        self.assertIn(f"CYCLE_PATH = '{CYCLE_PATH}'", js)
        self.assertIn("if (!cycle) return 'UNAVAILABLE'", js)
        self.assertIn("if (cycle.schema !== CYCLE_SCHEMA) return 'UNAVAILABLE'", js)
        self.assertIn("if (cycle.productionPromotion === true) return 'DENY'", js)
        self.assertIn(
            "if (cycle.lambda !== 'CONJECTURE_1' || cycle.lambdaNeverATheorem !== true) return 'DENY'",
            js,
        )
        self.assertIn("if (cycle.authority !== 'PROPOSAL_ONLY') return 'DENY'", js)
        self.assertIn("if (cycle.invariantsOk !== true) return 'DENY'", js)
        self.assertIn("if (cycle.shadow && cycle.shadow.executable === true) return 'DENY'", js)
        self.assertIn("if (cycle.verdict === 'ALLOW') return 'ALLOW'", js)
        self.assertIn("cycle.verdict === 'HARD_DENY'", js)
        self.assertIn("cycle.verdict === 'LAMBDA_VETO'", js)
        self.assertIn("cycle.verdict === 'DENY_DEFAULT'", js)
        self.assertIn("cycle.verdict === 'ESCALATE'", js)
        self.assertIn("fetchImpl(CYCLE_PATH", js)
        self.assertNotIn("workflow_dispatch", js)

    def test_hud_binds_paint_and_never_mints_arithmetic_control(self):
        app = (ROOT / "assets" / "app.js").read_text(encoding="utf-8")
        page = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn("from './paint.js'", app)
        self.assertIn("loadPublishedCycle", app)
        self.assertIn("paintFromCycle", app)
        self.assertIn("allowChrome", app)
        self.assertIn("applyOrganPaint", app)
        self.assertIn("data-paint", app)
        self.assertIn('data-paint="UNAVAILABLE"', page)
        self.assertIn('id="organ-paint"', page)
        self.assertIn('id="organ-paint-label"', page)
        self.assertIn('id="organ-verdict"', page)
        self.assertIn('id="organ-cycle-note"', page)
        self.assertIn('id="inspect-evidence"', page)
        self.assertIn("UNAVAILABLE until a bound cycle receipt is loaded", page)
        self.assertNotIn('id="arithmetic-allow"', page)
        self.assertNotRegex(page.lower(), r"<button[^>]*(allow|promote|execute)")
        self.assertNotIn("arithmeticScore", app)
        self.assertNotIn("productionPromotion = true", app)
        self.assertNotRegex(app, r"btn-primary.*arithmetic")
        inspect = [line for line in page.splitlines() if 'id="inspect-evidence"' in line][0]
        self.assertIn("btn-ghost", inspect)
        self.assertNotIn("btn-primary", inspect)

    def test_space_proxies_organ_cycle_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            handler = functools.partial(server.HardenedHandler, directory=directory)
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{httpd.server_port}{CYCLE_PATH}"
            try:
                with patch("server.load_published_cycle", return_value=allowing_cycle()):
                    with urlopen(url, timeout=3) as response:
                        self.assertEqual(200, response.status)
                        self.assertEqual("no-store", response.headers["Cache-Control"])
                        payload = json.load(response)
                self.assertEqual(CYCLE_SCHEMA, payload["schema"])
                self.assertEqual("ALLOW", paint_from_cycle(payload))

                with patch("server.load_published_cycle", return_value=None):
                    with self.assertRaises(HTTPError) as caught:
                        urlopen(url, timeout=3)
                self.assertEqual(503, caught.exception.code)
                self.assertEqual("UNAVAILABLE", paint_from_cycle(json.load(caught.exception)))
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=3)

    def test_proxy_targets_published_organ_and_workflow_stays_push_only(self):
        self.assertEqual(
            "https://szlholdings-szl-frontier.hf.space/frontier/ouroboros-cycle.v1.json",
            server.ORGAN_CYCLE_URL,
        )
        self.assertEqual(ORGAN_CYCLE_URL, server.ORGAN_CYCLE_URL)
        self.assertEqual(CYCLE_PATH, server.CYCLE_PATH)
        workflow = (REPO / ".github" / "workflows" / "hf-space-deploy.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("workflow_dispatch", workflow)
        self.assertIn("space/**", workflow)


if __name__ == "__main__":
    unittest.main()
