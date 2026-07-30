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
import re
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from szl_source_attestation import build_attestation

PORT = 7860
DIRECTORY = "/app"
SPACE_ID = "SZLHOLDINGS/llm-router-live"
SNAPSHOT_MAX_AGE_SECONDS = 24 * 60 * 60
SOURCE_BINDING_FILENAME = "SOURCE_BINDING.json"
_SHA = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

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


def classify_snapshot_freshness(captured_at, *, now=None):
    """Return an explicit freshness boundary for a snapshot timestamp.

    Missing, malformed, timezone-naive, or future timestamps are UNKNOWN rather
    than optimistically treated as fresh.
    """
    result = {
        "freshness_state": "UNKNOWN",
        "snapshot_age_seconds": None,
        "stale_after_seconds": SNAPSHOT_MAX_AGE_SECONDS,
    }
    if not isinstance(captured_at, str) or not captured_at.strip():
        return result

    try:
        captured = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except ValueError:
        return result
    if captured.tzinfo is None:
        return result

    observed_now = now or datetime.now(timezone.utc)
    if observed_now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    age_seconds = int(
        (observed_now.astimezone(timezone.utc) - captured.astimezone(timezone.utc)).total_seconds()
    )
    if age_seconds < 0:
        return result

    result["snapshot_age_seconds"] = age_seconds
    result["freshness_state"] = (
        "FRESH" if age_seconds <= SNAPSHOT_MAX_AGE_SECONDS else "STALE"
    )
    return result


def load_source_binding(directory):
    unavailable = {
        "repository": None,
        "commit": None,
        "path": "space",
        "relation": "exact-deployed-subtree",
        "state": "UNAVAILABLE",
        "evidence_url": None,
    }
    try:
        payload = json.loads(
            (Path(directory) / SOURCE_BINDING_FILENAME).read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return unavailable

    repository = str(payload.get("source_repository", "")).strip()
    revision = str(payload.get("source_revision", "")).strip().lower()
    if (
        payload.get("schema") != "szl.source-binding/v1"
        or not _REPOSITORY.fullmatch(repository)
        or not _SHA.fullmatch(revision)
        or payload.get("source_path") != "space"
        or payload.get("relation") != "exact-deployed-subtree"
    ):
        return unavailable
    return {
        "repository": repository,
        "commit": revision,
        "path": "space",
        "relation": "exact-deployed-subtree",
        "state": "SOURCE_BOUND",
        "evidence_url": (
            f"https://github.com/{repository}/tree/{revision}/space"
        ),
    }


def build_source_attestation(directory, *, force=False):
    source = load_source_binding(directory)
    source_bound = source["state"] == "SOURCE_BOUND"
    payload = build_attestation(
        SPACE_ID,
        source,
        "SOURCE_BOUND_DEPLOYMENT" if source_bound else "UNAVAILABLE",
        force=force,
    )
    if source_bound:
        payload["verification_state"] = "SOURCE_BOUND"
        payload["claims"]["github_parity"] = "EXACT_DEPLOYED_SUBTREE"
        payload["limits"] = [
            "GitHub parity is scoped to the szl-router/space subtree.",
            "The deployment workflow verifies each shipped subtree file by SHA-256.",
            "No reproducible-build or router-gateway runtime claim is made.",
        ]
    return payload


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

    def _send_json(self, payload, *, status=200):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-SZL-Transport-State", str(payload["transport_state"]))
        self.send_header("X-SZL-Evidence-State", str(payload["evidence_state"]))
        self.send_header("X-SZL-Verification-State", str(payload["verification_state"]))
        self.send_header("X-SZL-Authority-State", str(payload["authority_state"]))
        self.end_headers()
        self.wfile.write(body)

    def _send_snapshot_json(self, path):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("snapshot root must be an object")
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            self.send_error(503, "snapshot unavailable")
            return

        payload.update(classify_snapshot_freshness(payload.get("captured_at")))
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-SZL-Transport-State", "REACHABLE")
        self.send_header("X-SZL-Evidence-State", "SNAPSHOT")
        self.send_header("X-SZL-Freshness-State", payload["freshness_state"])
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        directory = self.directory or DIRECTORY
        if parsed.path == "/.well-known/szl-source.json":
            force = urllib.parse.parse_qs(parsed.query).get("refresh") == ["1"]
            self._send_json(
                build_source_attestation(directory, force=force)
            )
            return
        if parsed.path in {"/healthz", "/readyz", "/api/build-info"}:
            source = load_source_binding(directory)
            source_bound = source["state"] == "SOURCE_BOUND"
            common = {
                "transport_state": "REACHABLE",
                "evidence_state": "OBSERVED" if source_bound else "UNAVAILABLE",
                "verification_state": "SOURCE_BOUND" if source_bound else "UNAVAILABLE",
                "authority_state": "READ_ONLY",
            }
            if parsed.path == "/api/build-info":
                self._send_json(
                    {
                        **common,
                        "service": "llm-router-live",
                        "version": "1.0",
                        "build": {
                            "state": "OBSERVED" if source_bound else "UNAVAILABLE",
                            "revision": source["commit"],
                            "repository": source["repository"],
                            "path": source["path"],
                        },
                        "source_revision": source["commit"],
                        "source_revision_state": (
                            "OBSERVED" if source_bound else "UNAVAILABLE"
                        ),
                        "github_huggingface_alignment": (
                            "SOURCE_BOUND_DEPLOYMENT" if source_bound else "UNAVAILABLE"
                        ),
                        "receipt_minted": False,
                    },
                    status=200 if source_bound else 503,
                )
                return
            payload = {
                **common,
                "status": "ready" if source_bound else "degraded",
                "service": "llm-router-live",
                "access_mode": "PUBLIC_READONLY",
                "router_runtime": "NOT_MEASURED",
                "source_binding": source["state"],
            }
            status = 200 if parsed.path == "/healthz" or source_bound else 503
            self._send_json(payload, status=status)
            return
        routes = {
            "/api/a11oy/v1/router/health": "snapshot-router-health.json",
            "/api/a11oy/v1/router/models": "snapshot-router-models.json",
            "/api/a11oy/v1/router/provenance": "snapshot-router-provenance.json",
        }
        if parsed.path in routes:
            path = Path(self.directory or DIRECTORY) / "assets" / routes[parsed.path]
            self._send_snapshot_json(path)
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
