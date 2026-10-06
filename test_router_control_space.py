"""Offline contract tests for the separate router control Space publisher."""
from __future__ import annotations

import json
import hashlib
import tempfile
import threading
import unittest
import io
import os
import sys
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from huggingface_hub.errors import RepositoryNotFoundError

from scripts import router_control_space as control


SOURCE = "a" * 40
IMAGE_DIGEST = "sha256:" + "b" * 64
PARENT = "c" * 40
PUBLISHED = "d" * 40
PROVIDER_METADATA = b"*.bin filter=lfs diff=lfs merge=lfs -text\n"


class FixtureNotFound(RepositoryNotFoundError):
    def __init__(self, status_code=404):
        Exception.__init__(self, "fixture not found")
        self.response = SimpleNamespace(status_code=status_code)


class FakeApi:
    def __init__(self, *, exists: bool = False, files: dict[str, bytes] | None = None):
        self.exists = exists
        self.sha = PARENT
        self.private = False
        self.files = files or {}
        self.created = False
        self.uploaded = False

    def repo_info(self, *, repo_id: str, repo_type: str):
        assert repo_id == control.TARGET and repo_type == "space"
        if not self.exists:
            raise FixtureNotFound()
        return SimpleNamespace(id=repo_id, sha=self.sha, private=self.private, sdk="docker")

    def create_repo(self, **kwargs):
        assert kwargs == {"repo_id": control.TARGET, "repo_type": "space",
                          "private": False, "exist_ok": False, "space_sdk": "docker"}
        self.created = True
        self.exists = True
        # The Hub initial system commit supplies a persistent .gitattributes.
        self.files = {control.PROVIDER_METADATA: PROVIDER_METADATA}

    def auth_check(self, **kwargs):
        assert kwargs == {"repo_id": control.TARGET, "repo_type": "space", "write": True}

    def upload_folder(self, **kwargs):
        assert kwargs["repo_id"] == control.TARGET
        assert kwargs["repo_type"] == "space"
        assert kwargs["parent_commit"] == PARENT
        assert kwargs["delete_patterns"] == "*"
        folder = Path(kwargs["folder_path"])
        self.files = {
            control.PROVIDER_METADATA: self.files[control.PROVIDER_METADATA],
            **{path.name: path.read_bytes() for path in folder.iterdir()},
        }
        self.uploaded = True
        self.sha = PUBLISHED
        return SimpleNamespace(oid=PUBLISHED)

    def list_repo_files(self, **kwargs):
        assert kwargs == {"repo_id": control.TARGET, "repo_type": "space", "revision": self.sha}
        return list(self.files)


class SpacePublisherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        validator = patch.object(control, "validate_card")
        self.card_validator = validator.start()
        self.addCleanup(validator.stop)

    def download(self, api: FakeApi):
        def fake_download(*, repo_id, filename, repo_type, revision, token):
            self.assertEqual((repo_id, repo_type, revision, token),
                             (control.TARGET, "space", api.sha, "fixture-token"))
            path = Path(self.temporary.name) / filename
            path.write_bytes(api.files[filename])
            return str(path)
        return fake_download

    def run_publish(self, api: FakeApi, *, runtime_aligned: bool = True):
        with patch.object(control, "require_protected_main"), \
             patch.object(control, "anonymous_image_manifest"), \
             patch.object(control, "_runtime_aligned", return_value=runtime_aligned), \
             patch.object(control, "_witness") as witness:
            result = control.publish(api, self.download(api), token="fixture-token",
                                     source_revision=SOURCE, image_digest=IMAGE_DIGEST,
                                     timeout_seconds=0)
        witness.assert_called_once_with(SOURCE)
        return result

    def test_bundle_is_closed_and_image_pinned_with_egress_denied(self):
        from huggingface_hub import SpaceCard

        files = control.space_files(SOURCE, IMAGE_DIGEST)
        self.assertEqual(set(files), control.FILES)
        binding = json.loads(files["SOURCE_BINDING.json"])
        self.assertEqual(binding["target"], control.TARGET)
        self.assertEqual(binding["source_revision"], SOURCE)
        self.assertEqual(binding["image_reference"], f"{control.IMAGE}@{IMAGE_DIGEST}")
        self.assertEqual(binding["egress_policy"], "DENIED")
        self.assertIn(f"FROM {control.IMAGE}@{IMAGE_DIGEST}".encode(), files["Dockerfile"])
        self.assertIn(b"SZL_ROUTER_ENABLE_EGRESS=0", files["Dockerfile"])
        self.assertNotIn(b"HF_TOKEN", b"".join(files.values()))
        self.assertNotIn(b"SZLHOLDINGS/llm-router-live\nFROM", b"".join(files.values()))
        card = SpaceCard(files["README.md"].decode())
        self.assertLessEqual(len(card.data.short_description), 60)

    def test_invalid_card_cannot_create_or_upload_target(self):
        api = FakeApi()
        self.card_validator.side_effect = control.ControlSpaceError("SPACE_CARD_METADATA_INVALID")
        with patch.object(control, "require_protected_main"), \
             patch.object(control, "anonymous_image_manifest"):
            with self.assertRaisesRegex(control.ControlSpaceError, "SPACE_CARD_METADATA_INVALID"):
                control.publish(api, self.download(api), token="fixture-token",
                                source_revision=SOURCE, image_digest=IMAGE_DIGEST)
        self.assertFalse(api.created)
        self.assertFalse(api.uploaded)

    def test_recorded_bootstrap_recovery_requires_exact_revision_tree_and_bytes(self):
        scaffold = {control.PROVIDER_METADATA: PROVIDER_METADATA,
                    "README.md": b"system-created card\n"}
        binding = {
            "schema": "szl.router-control-bootstrap/v1", "target": control.TARGET,
            "source_repository": control.SOURCE_REPOSITORY, "parent_revision": PARENT,
            "files_sha256": {name: hashlib.sha256(body).hexdigest() for name, body in scaffold.items()},
        }
        binding_path = Path(self.temporary.name) / "bootstrap.json"
        binding_path.write_text(json.dumps(binding))
        with patch.object(control, "BOOTSTRAP_BINDING", binding_path):
            api = FakeApi(exists=True, files=dict(scaffold))
            result = self.run_publish(api)
            self.assertEqual(result["status"], "MEASURED")
            self.assertFalse(api.created)
            for mutation in ("revision", "readme", "extra_file"):
                with self.subTest(mutation=mutation):
                    api = FakeApi(exists=True, files=dict(scaffold))
                    if mutation == "revision":
                        api.sha = "e" * 40
                    elif mutation == "readme":
                        api.files["README.md"] += b"changed"
                    else:
                        api.files["app.py"] = b"unowned app"
                    with patch.object(control, "require_protected_main"), \
                         patch.object(control, "anonymous_image_manifest"):
                        with self.assertRaises(control.ControlSpaceError):
                            control.publish(api, self.download(api), token="fixture-token",
                                            source_revision=SOURCE, image_digest=IMAGE_DIGEST)
                    self.assertFalse(api.created)
                    self.assertFalse(api.uploaded)

    def test_bootstrap_exact_target_cas_and_byte_readback(self):
        api = FakeApi()
        result = self.run_publish(api)
        self.assertTrue(api.created)
        self.assertTrue(api.uploaded)
        self.assertEqual(result["status"], "MEASURED")
        self.assertEqual(result["published_revision"], PUBLISHED)
        self.assertEqual(set(api.files), control.FILES | {control.PROVIDER_METADATA})
        self.assertEqual(api.files[control.PROVIDER_METADATA], PROVIDER_METADATA)
        self.assertEqual(result["provider_metadata_sha256"], hashlib.sha256(PROVIDER_METADATA).hexdigest())

    def test_provider_metadata_must_remain_byte_identical(self):
        api = FakeApi()
        original_upload = api.upload_folder

        def tampered_upload(**kwargs):
            commit = original_upload(**kwargs)
            api.files[control.PROVIDER_METADATA] = b"changed by provider\n"
            return commit

        api.upload_folder = tampered_upload
        with patch.object(control, "require_protected_main"), \
             patch.object(control, "anonymous_image_manifest"), \
             patch.object(control, "_runtime_aligned", return_value=True), \
             patch.object(control, "_witness") as witness:
            with self.assertRaises(control.ControlSpaceError) as caught:
                control.publish(api, self.download(api), token="fixture-token",
                                source_revision=SOURCE, image_digest=IMAGE_DIGEST)
        self.assertEqual(caught.exception.code, "SPACE_PROVIDER_METADATA_DRIFT")
        witness.assert_not_called()

    def test_existing_exact_space_can_update_without_changing_provider_metadata(self):
        files = control.space_files(SOURCE, IMAGE_DIGEST)
        api = FakeApi(exists=True, files={control.PROVIDER_METADATA: PROVIDER_METADATA, **files})
        result = self.run_publish(api)
        self.assertFalse(api.created)
        self.assertTrue(api.uploaded)
        self.assertFalse(result["space_created"])
        self.assertEqual(api.files[control.PROVIDER_METADATA], PROVIDER_METADATA)

    def test_existing_unowned_target_is_never_overwritten(self):
        api = FakeApi(exists=True, files={"README.md": b"other owner's Space"})
        with patch.object(control, "require_protected_main"), \
             patch.object(control, "anonymous_image_manifest"):
            with self.assertRaises(control.ControlSpaceError) as caught:
                control.publish(api, self.download(api), token="fixture-token",
                                source_revision=SOURCE, image_digest=IMAGE_DIGEST)
        self.assertEqual(caught.exception.code, "EXISTING_SPACE_FILE_SET_REJECTED")
        self.assertFalse(api.uploaded)

    def test_runtime_not_aligned_cannot_be_called_published(self):
        api = FakeApi()
        attempt = {}
        with patch.object(control, "require_protected_main"), \
             patch.object(control, "anonymous_image_manifest"), \
             patch.object(control, "_runtime_aligned", return_value=False), \
             patch.object(control, "_witness") as witness:
            with self.assertRaises(control.ControlSpaceError) as caught:
                control.publish(api, self.download(api), token="fixture-token",
                                source_revision=SOURCE, image_digest=IMAGE_DIGEST,
                                timeout_seconds=0, attempt=attempt)
        self.assertEqual(caught.exception.code, "SPACE_RUNTIME_TIMEOUT")
        witness.assert_not_called()
        self.assertEqual(attempt["published_revision"], PUBLISHED)
        self.assertEqual(attempt["parent_revision"], PARENT)
        self.assertEqual(attempt["write_outcome"], "MEASURED")
        self.assertTrue(attempt["space_created"])

    def test_denied_or_unknown_access_never_attempts_creation(self):
        for status in (401, 403, None):
            with self.subTest(status=status):
                api = FakeApi()
                with patch.object(api, "repo_info", side_effect=FixtureNotFound(status)), \
                     patch.object(control, "require_protected_main"), \
                     patch.object(control, "anonymous_image_manifest"):
                    with self.assertRaisesRegex(control.ControlSpaceError, "SPACE_ACCESS_REJECTED"):
                        control.publish(api, self.download(api), token="fixture-token",
                                        source_revision=SOURCE, image_digest=IMAGE_DIGEST)
                self.assertFalse(api.created)
                self.assertFalse(api.uploaded)

    def test_lost_upload_response_retains_unknown_effect_and_binding(self):
        api = FakeApi()
        attempt = {}
        snapshots = []
        with patch.object(control, "require_protected_main"), \
             patch.object(control, "anonymous_image_manifest"), \
             patch.object(api, "upload_folder", side_effect=TimeoutError("private detail")):
            with self.assertRaises(TimeoutError):
                control.publish(api, self.download(api), token="fixture-token",
                                source_revision=SOURCE, image_digest=IMAGE_DIGEST,
                                attempt=attempt, checkpoint=lambda value: snapshots.append(dict(value)))
        self.assertEqual(attempt["phase"], "UPLOAD_REQUESTED")
        self.assertEqual(attempt["write_outcome"], "UNKNOWN")
        self.assertEqual(attempt["source_revision"], SOURCE)
        self.assertEqual(attempt["image_digest"], IMAGE_DIGEST)
        self.assertEqual(attempt["parent_revision"], PARENT)
        self.assertTrue(any(value["phase"] == "CREATE_REQUESTED" for value in snapshots))
        self.assertTrue(any(value["phase"] == "UPLOAD_REQUESTED" for value in snapshots))

    def test_auth_check_failure_prevents_upload(self):
        api = FakeApi(exists=True, files={control.PROVIDER_METADATA: PROVIDER_METADATA,
                                        **control.space_files(SOURCE, IMAGE_DIGEST)})
        with patch.object(control, "require_protected_main"), \
             patch.object(control, "anonymous_image_manifest"), \
             patch.object(api, "auth_check", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                control.publish(api, self.download(api), token="fixture-token",
                                source_revision=SOURCE, image_digest=IMAGE_DIGEST)
        self.assertFalse(api.uploaded)

    def test_cli_failure_preserves_effect_receipt_without_secret(self):
        receipt_path = Path(self.temporary.name) / "receipt.json"

        def failed_publish(*_args, attempt, checkpoint, **kwargs):
            self.assertEqual(kwargs["token"], "test-only-secret")
            attempt.update(phase="UPLOADED", published_revision=PUBLISHED,
                           parent_revision=PARENT, write_outcome="MEASURED")
            checkpoint(attempt)
            raise control.ControlSpaceError("SPACE_RUNTIME_TIMEOUT")

        arguments = ["publisher", "--source-revision", SOURCE, "--image-digest",
                     IMAGE_DIGEST, "--receipt", str(receipt_path)]
        with patch.object(sys, "argv", arguments), \
             patch.dict(os.environ, {"HF_TOKEN": "test-only-secret"}), \
             patch.object(control, "publish", side_effect=failed_publish), \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(control.main(), 1)
        receipt = json.loads(receipt_path.read_bytes())
        self.assertEqual(receipt["status"], "BLOCKED")
        self.assertEqual(receipt["published_revision"], PUBLISHED)
        self.assertEqual(receipt["parent_revision"], PARENT)
        self.assertEqual(receipt["image_digest"], IMAGE_DIGEST)
        self.assertEqual(receipt["failure_code"], "SPACE_RUNTIME_TIMEOUT")
        self.assertNotIn("test-only-secret", receipt_path.read_text() + output.getvalue())

    def test_runtime_witness_matches_real_default_deny_app(self):
        from fastapi.testclient import TestClient
        from router_control.app import app

        with patch.dict(os.environ, {"SOURCE_REVISION": SOURCE}, clear=True):
            with TestClient(app) as client:
                def request_json(url, *, status=200, no_store=False):
                    response = client.get(url.removeprefix(control.ENDPOINT))
                    self.assertEqual(response.status_code, status)
                    if no_store:
                        self.assertIn("no-store", response.headers.get("Cache-Control", ""))
                    return response.json()

                with patch.object(control, "_request_json", side_effect=request_json):
                    control._witness(SOURCE)

    def test_wrong_source_or_image_is_rejected_before_provider_use(self):
        api = FakeApi()
        for revision, digest in (("short", IMAGE_DIGEST), (SOURCE, "sha256:short")):
            with self.subTest(revision=revision, digest=digest):
                with self.assertRaises(control.ControlSpaceError):
                    control.publish(api, self.download(api), token="fixture-token",
                                    source_revision=revision, image_digest=digest)
                self.assertFalse(api.created)

    def test_authenticated_http_redirect_never_reaches_new_origin(self):
        seen: dict[str, list[str | None]] = {"first": [], "second": []}

        class Target(BaseHTTPRequestHandler):
            def do_GET(self):
                seen["second"].append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *_args):
                pass

        target = ThreadingHTTPServer(("127.0.0.1", 0), Target)

        class Redirect(BaseHTTPRequestHandler):
            def do_GET(self):
                seen["first"].append(self.headers.get("Authorization"))
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target.server_port}/capture")
                self.end_headers()

            def log_message(self, *_args):
                pass

        redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        threads = [threading.Thread(target=server.serve_forever, daemon=True)
                   for server in (target, redirect)]
        for thread in threads:
            thread.start()
        try:
            with self.assertRaises(control.ControlSpaceError) as caught:
                control._request_json(f"http://127.0.0.1:{redirect.server_port}/start",
                                      token="test-only-secret")
            self.assertEqual(caught.exception.code, "HTTP_WITNESS_REJECTED")
            self.assertEqual(seen["first"], ["Bearer test-only-secret"])
            self.assertEqual(seen["second"], [])
        finally:
            for server in (redirect, target):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
