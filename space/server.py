#!/usr/bin/env python3
"""Hardened static file server for the SZL LLM Router (public status) Space.

Serves exactly the same files as `python -m http.server 7860` (same directory,
same port) but adds security response headers on every response:
  - Content-Security-Policy   (sane static policy, tuned to this page's assets)
  - Strict-Transport-Security (max-age=31536000; includeSubDomains)
  - X-Content-Type-Options    (nosniff)
  - Referrer-Policy           (strict-origin-when-cross-origin)

Also suppresses the default "SimpleHTTP/x Python/y" Server banner (P2 info-leak
fix) by returning a clean, versionless Server string.

Additive / non-breaking: no ports or file paths are changed. The CSP is
constructed to permit every resource this Space actually uses (inline scripts
incl. the SZLVerify.mount() bootstrap, inline styles/attributes, the assets/
logo.svg favicon, the self-hosted app.js + verify widget, the bundled snapshot
JSON, and the live status/verify fetches to a-11-oy.com), so the live status HUD
and the verify widget keep working.
"""
import functools
import json
import urllib.parse
from pathlib import Path
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from szl_source_attestation import build_attestation

PORT = 7860
DIRECTORY = "/app"
SPACE_ID = "SZLHOLDINGS/llm-router-live"
SOURCE_OBSERVATION = {
    "repository": "szl-holdings/szl-router",
    "commit": "df23a589f0365afa5bdd71da2997941301065535",
    "path": "",
    "relation": "backend-concept-not-space-mirror",
    "state": "VERIFIED_REFERENCE",
    "evidence_url": "https://github.com/szl-holdings/szl-router/commit/df23a589f0365afa5bdd71da2997941301065535",
}

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "base-uri 'self'; "
    "object-src 'none'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self'; "
    "connect-src 'self' https://a-11-oy.com; "
    "frame-ancestors 'self' https://huggingface.co https://*.hf.space https://*.huggingface.co"
)


class HardenedHandler(SimpleHTTPRequestHandler):
    def version_string(self):
        return "SZL"

    def end_headers(self):
        self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        self.send_header(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        super().end_headers()

    def _send_json(self, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-SZL-Transport-State", str(payload["transport_state"]))
        self.send_header("X-SZL-Evidence-State", str(payload["evidence_state"]))
        self.send_header("X-SZL-Verification-State", str(payload["verification_state"]))
        self.send_header("X-SZL-Authority-State", str(payload["authority_state"]))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/.well-known/szl-source.json":
            force = urllib.parse.parse_qs(parsed.query).get("refresh") == ["1"]
            self._send_json(
                build_attestation(
                    SPACE_ID,
                    SOURCE_OBSERVATION,
                    "NOT_A_DIRECT_MIRROR",
                    force=force,
                )
            )
            return
        routes = {
            "/api/a11oy/v1/router/health": "snapshot-router-health.json",
            "/api/a11oy/v1/router/models": "snapshot-router-models.json",
            "/api/a11oy/v1/router/provenance": "snapshot-router-provenance.json",
        }
        if parsed.path in routes:
            path = Path(self.directory or DIRECTORY) / "assets" / routes[parsed.path]
            try:
                payload = path.read_bytes()
            except OSError:
                self.send_error(503, "snapshot unavailable")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-SZL-Transport-State", "REACHABLE")
            self.send_header("X-SZL-Evidence-State", "SNAPSHOT")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        super().do_GET()


if __name__ == "__main__":
    handler = functools.partial(HardenedHandler, directory=DIRECTORY)
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), handler)
    print(f"Serving hardened static site from {DIRECTORY} on 0.0.0.0:{PORT}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.server_close()
