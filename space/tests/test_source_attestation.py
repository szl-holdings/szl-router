import functools
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server  # noqa: E402


class SourceAttestationTests(unittest.TestCase):
    @staticmethod
    def _write_binding(directory, revision="c" * 40):
        (Path(directory) / "SOURCE_BINDING.json").write_text(
            json.dumps(
                {
                    "schema": "szl.source-binding/v1",
                    "source_repository": "szl-holdings/szl-router",
                    "source_revision": revision,
                    "source_path": "space",
                    "relation": "exact-deployed-subtree",
                }
            ),
            encoding="utf-8",
        )

    def test_exact_subtree_binding_is_reported_without_build_overclaim(self):
        with tempfile.TemporaryDirectory() as directory:
            self._write_binding(directory)
            with patch.dict(os.environ, {"SPACE_REPOSITORY_COMMIT": "a" * 40}):
                payload = server.build_source_attestation(directory)
        self.assertEqual("a" * 40, payload["deployment"]["hf_revision"])
        self.assertEqual("c" * 40, payload["source"]["commit"])
        self.assertEqual("SOURCE_BOUND", payload["source"]["state"])
        self.assertEqual("exact-deployed-subtree", payload["source"]["relation"])
        self.assertEqual("SOURCE_BOUND_DEPLOYMENT", payload["alignment_state"])
        self.assertEqual("EXACT_DEPLOYED_SUBTREE", payload["claims"]["github_parity"])
        self.assertEqual("NOT_CLAIMED", payload["claims"]["reproducible_build"])

    def test_well_known_route_returns_uncacheable_json(self):
        with tempfile.TemporaryDirectory() as directory:
            self._write_binding(directory)
            handler = functools.partial(server.HardenedHandler, directory=directory)
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.dict(os.environ, {"SPACE_REPOSITORY_COMMIT": "b" * 40}):
                    with urlopen(
                        f"http://127.0.0.1:{httpd.server_port}/.well-known/szl-source.json",
                        timeout=3,
                    ) as response:
                        payload = json.load(response)
                        self.assertEqual(200, response.status)
                        self.assertEqual("no-store", response.headers["Cache-Control"])
                        self.assertEqual("SOURCE_BOUND", response.headers["X-SZL-Verification-State"])
                        self.assertEqual("READ_ONLY", response.headers["X-SZL-Authority-State"])
                        self.assertEqual("b" * 40, payload["deployment"]["hf_revision"])
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=3)

    def test_readiness_fails_closed_without_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            handler = functools.partial(server.HardenedHandler, directory=directory)
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                with self.assertRaisesRegex(Exception, "503"):
                    urlopen(
                        f"http://127.0.0.1:{httpd.server_port}/readyz",
                        timeout=3,
                    )
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
