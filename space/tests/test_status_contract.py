import functools
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server  # noqa: E402


class StatusContractTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        handler = functools.partial(server.HardenedHandler, directory=self.directory.name)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        self.directory.cleanup()

    def _get(self, path):
        with urlopen(self.base + path, timeout=3) as response:
            return response.status, json.load(response)

    def test_health_is_process_liveness(self):
        status, payload = self._get("/health")
        self.assertEqual(200, status)
        self.assertEqual("RUNNING", payload["status"])
        self.assertFalse(payload["credential_value_recorded"])

    def test_inference_ready_is_offline_until_keyed(self):
        with self.assertRaises(HTTPError) as caught:
            urlopen(self.base + "/inference/ready", timeout=3)
        self.assertEqual(503, caught.exception.code)
        payload = json.load(caught.exception)
        self.assertEqual("OFFLINE_UNTIL_KEYED", payload["status"])

    def test_models_list_logical_routes(self):
        status, payload = self._get("/v1/models")
        self.assertEqual(200, status)
        ids = {item["id"] for item in payload["data"]}
        self.assertEqual({"szl-auto", "szl-fast", "szl-large", "szl-coder"}, ids)
        self.assertEqual("OFFLINE_UNTIL_KEYED", payload["inference_state"])

    def test_chat_completions_do_not_proxy(self):
        req = Request(
            self.base + "/v1/chat/completions",
            data=b'{"model":"szl-large","messages":[{"role":"user","content":"hi"}]}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(req, timeout=3)
        self.assertEqual(503, caught.exception.code)
        payload = json.load(caught.exception)
        self.assertEqual("offline_until_keyed", payload["error"]["type"])
        self.assertFalse(payload["credential_value_recorded"])


if __name__ == "__main__":
    unittest.main()
