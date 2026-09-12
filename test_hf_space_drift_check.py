import argparse
import copy
import json
import re
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts import hf_space_drift_check as drift
from scripts import hf_space_deploy as deploy
from test_hf_space_deploy import commit_fixture


SOURCE_SHA = "a" * 40
HF_SHA = "b" * 40
OTHER_HF_SHA = "c" * 40
REPO_ID = "SZLHOLDINGS/llm-router-live"
ENDPOINT = "https://szlholdings-llm-router-live.hf.space"


class FakeClient:
    def __init__(self, json_responses, byte_responses):
        self.json_responses = {
            url: value if isinstance(value, BaseException) else copy.deepcopy(value)
            for url, value in json_responses.items()
        }
        self.byte_responses = copy.deepcopy(byte_responses)

    @staticmethod
    def _take(responses, url):
        if url not in responses:
            raise AssertionError(f"Unexpected URL: {url}")
        value = responses[url]
        if isinstance(value, list):
            if not value:
                raise AssertionError(f"No response remains for URL: {url}")
            value = value.pop(0)
        if isinstance(value, BaseException):
            raise value
        return copy.deepcopy(value)

    def get_json(self, url, *, timeout, authenticate):
        del timeout, authenticate
        return self._take(self.json_responses, url), 200

    def get_json_array(self, url, *, timeout, authenticate):
        del timeout, authenticate
        if url not in self.json_responses:
            raise AssertionError(f"Unexpected URL: {url}")
        return copy.deepcopy(self.json_responses[url]), 200

    def get_witness_json(self, url, *, timeout):
        del timeout
        return self._take(self.json_responses, url), 200, {
            "cache-control": "no-store",
            "age": "0",
        }

    def get_bytes(self, url, *, timeout):
        del timeout
        return self._take(self.byte_responses, url), 200


class DriftVerifierTests(unittest.TestCase):
    def _fixture(self):
        temporary = TemporaryDirectory()
        source_root = Path(temporary.name)
        space_dir = source_root / "space"
        space_dir.mkdir()
        content = b"exact publication bytes\n"
        (space_dir / "index.html").write_bytes(content)
        (source_root / ".gitignore").write_text("__pycache__/\n*.pyc\n.env\n", encoding="utf-8")
        source_sha = commit_fixture(source_root)
        config = {
            "repo_id": REPO_ID,
            "endpoint": ENDPOINT,
            "endpoint_hostname": "szlholdings-llm-router-live.hf.space",
            "source_repository": drift.SOURCE_REPOSITORY,
            "source_revision": source_sha,
            "checkout_revision": source_sha,
            "source_root": source_root,
            "space_dir": space_dir,
            "token": "hf_test_token",
            "witness_timeout_seconds": 30.0,
            "poll_interval_seconds": 1.0,
            "request_timeout_seconds": 2.0,
            "witness_nonce": "unit-test-nonce",
        }
        info = {"sha": HF_SHA, "subdomain": "szlholdings-llm-router-live"}
        binding = deploy._source_binding(drift.SOURCE_REPOSITORY, source_sha)
        readiness = {
            "transport_state": "REACHABLE",
            "evidence_state": "OBSERVED",
            "verification_state": "SOURCE_BOUND",
            "authority_state": "READ_ONLY",
            "status": "ready",
            "service": "llm-router-live",
            "source_binding": "SOURCE_BOUND",
        }
        attestation = {
            "schema": "szl.deployment-source/v1",
            "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "transport_state": "REACHABLE",
            "verification_state": "SOURCE_BOUND",
            "authority_state": "READ_ONLY",
            "alignment_state": "SOURCE_BOUND_DEPLOYMENT",
            "source": {
                "repository": drift.SOURCE_REPOSITORY,
                "commit": source_sha,
                "path": "space",
                "relation": "exact-deployed-subtree",
                "state": "SOURCE_BOUND",
            },
            "deployment": {
                "hf_space": REPO_ID,
                "hf_revision": HF_SHA,
                "revision_state": "MEASURED",
                "measurement_method": "SPACE_REPOSITORY_COMMIT",
            },
        }
        json_responses = {
            drift._provider_info_url(REPO_ID): [info, info],
            drift._provider_runtime_url(REPO_ID): [
                {"stage": "RUNNING", "sha": HF_SHA},
                {"stage": "RUNNING", "sha": HF_SHA},
            ],
            drift._tree_url(REPO_ID, HF_SHA): [
                {"type": "file", "path": "SOURCE_BINDING.json"},
                {"type": "file", "path": "index.html"},
            ],
            f"{ENDPOINT}/readyz?nonce=unit-test-nonce": readiness,
            f"{ENDPOINT}/.well-known/szl-source.json?refresh=1&nonce=unit-test-nonce": attestation,
        }
        byte_responses = {
            drift._resolve_url(REPO_ID, HF_SHA, "index.html"): content,
            drift._resolve_url(
                REPO_ID, HF_SHA, drift.SOURCE_BINDING_FILENAME
            ): deploy._binding_bytes(binding),
        }
        return temporary, config, json_responses, byte_responses

    def test_acceptance_keeps_truth_layers_separate_and_revision_bound(self):
        temporary, config, json_responses, byte_responses = self._fixture()
        self.addCleanup(temporary.cleanup)
        evidence = drift.new_evidence(config)

        drift.verify_once(
            config,
            FakeClient(json_responses, byte_responses),
            evidence,
            lambda: 2.0,
        )

        self.assertEqual("ACCEPTED", evidence["verdict"])
        self.assertEqual(config["source_revision"], evidence["source"]["revision"])
        self.assertEqual(HF_SHA, evidence["publication"]["revision"])
        self.assertEqual("EXACT", evidence["publication"]["output_set_state"])
        self.assertEqual(HF_SHA, evidence["deployment"]["runtime_revision"])
        self.assertEqual(
            "EXACT_REVISION_ALIGNED",
            evidence["truth_separation"]["publication_state"],
        )
        self.assertEqual(
            "PROVIDER_RUNNING",
            evidence["truth_separation"]["deployment_state"],
        )
        self.assertEqual(
            "READY_WITNESSED",
            evidence["truth_separation"]["runtime_state"],
        )
        self.assertEqual("EXACT", evidence["bindings"]["source_to_publication"])
        self.assertEqual("EXACT", evidence["bindings"]["publication_to_runtime"])

    def test_generated_complete_binding_is_accepted(self):
        temporary, config, responses, bodies = self._fixture()
        self.addCleanup(temporary.cleanup)
        binding = deploy._source_binding(drift.SOURCE_REPOSITORY, config["source_revision"])
        self.assertEqual(
            {"schema", "source_repository", "source_revision", "source_path", "relation",
             "product_class", "public_space", "evidence_url"},
            set(binding),
        )
        url = drift._resolve_url(REPO_ID, HF_SHA, drift.SOURCE_BINDING_FILENAME)
        self.assertEqual(deploy._binding_bytes(binding), bodies[url])
        self.assertEqual(
            (json.dumps(binding, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            bodies[url],
        )
        evidence = drift.new_evidence(config)
        drift.verify_once(config, FakeClient(responses, bodies), evidence, lambda: 2.0)
        self.assertEqual("ACCEPTED", evidence["verdict"])

    def test_non_running_and_unknown_provider_stages_are_rejected(self):
        for stage in (
            "PAUSED",
            "BUILDING",
            "STARTING",
            "STOPPED",
            "ERROR",
            "SOMETHING_NEW",
        ):
            with self.subTest(stage=stage):
                temporary, config, json_responses, byte_responses = self._fixture()
                try:
                    json_responses[drift._provider_runtime_url(REPO_ID)] = [
                        {"stage": stage, "sha": HF_SHA}
                    ]
                    with self.assertRaises(drift.VerificationFailure) as caught:
                        drift.verify_once(
                            config,
                            FakeClient(json_responses, byte_responses),
                            drift.new_evidence(config),
                            lambda: 2.0,
                        )
                    self.assertEqual(
                        "PROVIDER_STAGE_NOT_RUNNING", caught.exception.code
                    )
                finally:
                    temporary.cleanup()

    def test_publisher_and_verifier_share_exact_output_despite_ignored_local_caches(self):
        temporary, config, json_responses, byte_responses = self._fixture()
        self.addCleanup(temporary.cleanup)
        space = config["space_dir"]
        cache = space / "__pycache__"
        cache.mkdir()
        for index in range(8):
            (cache / f"module{index}.cpython-312.pyc").write_bytes(b"bytecode")
        (space / ".env").write_bytes(b"TEST_ONLY=not-a-credential\n")
        snapshot = deploy._source_snapshot(space, config["source_revision"])
        release = config["source_root"] / "release"
        output = deploy._stage_release(
            release, snapshot, deploy._source_binding(drift.SOURCE_REPOSITORY, config["source_revision"])
        )
        json_responses[drift._tree_url(REPO_ID, HF_SHA)] = [
            {"type": "file", "path": relative} for relative in output
        ]
        byte_responses = {drift._resolve_url(REPO_ID, HF_SHA, path): body for path, body in output.items()}
        evidence = drift.new_evidence(config)
        drift.verify_once(config, FakeClient(json_responses, byte_responses), evidence, lambda: 2.0)
        self.assertEqual("ACCEPTED", evidence["verdict"])
        self.assertEqual(2, evidence["publication"]["expected_files"])
        json_responses[drift._tree_url(REPO_ID, HF_SHA)].append(
            {"type": "file", "path": "__pycache__/module0.cpython-312.pyc"}
        )
        with self.assertRaises(drift.VerificationFailure) as caught:
            drift.verify_once(config, FakeClient(json_responses, byte_responses), drift.new_evidence(config), lambda: 2.0)
        self.assertEqual("PUBLICATION_OUTPUT_SET_MISMATCH", caught.exception.code)

    def test_missing_duplicate_and_unsafe_remote_paths_are_rejected(self):
        for case in ("missing", "duplicate", "../escape", "/absolute", "a\\b", "a//b", "C:/drive", "a\ncontrol"):
            with self.subTest(case=case):
                temporary, config, responses, bodies = self._fixture()
                self.addCleanup(temporary.cleanup)
                tree = responses[drift._tree_url(REPO_ID, HF_SHA)]
                if case == "missing":
                    tree.pop()
                else:
                    tree.append({"type": "file", "path": "index.html" if case == "duplicate" else case})
                with self.assertRaises(drift.VerificationFailure) as caught:
                    drift.verify_once(config, FakeClient(responses, bodies), drift.new_evidence(config), lambda: 2.0)
                self.assertEqual(
                    "PUBLICATION_OUTPUT_SET_MISMATCH" if case == "missing" else "MALFORMED_PROVIDER_RESPONSE",
                    caught.exception.code,
                )

    def test_unsafe_and_unexpected_remote_directories_are_rejected(self):
        for path, code in (("../escape", "MALFORMED_PROVIDER_RESPONSE"),
                           ("empty-extra", "PUBLICATION_OUTPUT_SET_MISMATCH")):
            with self.subTest(path=path):
                temporary, config, responses, bodies = self._fixture()
                self.addCleanup(temporary.cleanup)
                responses[drift._tree_url(REPO_ID, HF_SHA)].append({"type": "directory", "path": path})
                with self.assertRaises(drift.VerificationFailure) as caught:
                    drift.verify_once(config, FakeClient(responses, bodies), drift.new_evidence(config), lambda: 2.0)
                self.assertEqual(code, caught.exception.code)

    def test_dirty_source_is_a_typed_verifier_failure(self):
        temporary, config, responses, bodies = self._fixture()
        self.addCleanup(temporary.cleanup)
        (config["space_dir"] / "index.html").write_bytes(b"modified")
        with self.assertRaises(drift.VerificationFailure) as caught:
            drift.verify_once(config, FakeClient(responses, bodies), drift.new_evidence(config), lambda: 2.0)
        self.assertEqual("SOURCE_WORKTREE_DIRTY", caught.exception.code)

    def test_malformed_provider_response_is_rejected(self):
        temporary, config, json_responses, byte_responses = self._fixture()
        self.addCleanup(temporary.cleanup)
        json_responses[drift._provider_info_url(REPO_ID)] = [{"subdomain": "x"}]
        with self.assertRaises(drift.VerificationFailure) as caught:
            drift.verify_once(
                config,
                FakeClient(json_responses, byte_responses),
                drift.new_evidence(config),
                lambda: 2.0,
            )
        self.assertEqual("MALFORMED_PROVIDER_RESPONSE", caught.exception.code)

    def test_tree_entry_type_json_shapes_are_typed_rejections(self):
        temporary, config, responses, bodies = self._fixture()
        self.addCleanup(temporary.cleanup)
        tree = responses[drift._tree_url(REPO_ID, HF_SHA)]
        for malformed in ([], {}, ["file"], {"file": True}, 42, None, True, "FILE"):
            with self.subTest(value=malformed):
                tree[0]["type"] = malformed
                with self.assertRaises(drift.VerificationFailure) as caught:
                    drift.verify_once(
                        config, FakeClient(responses, bodies),
                        drift.new_evidence(config), lambda: 2.0,
                    )
                self.assertEqual("MALFORMED_PROVIDER_RESPONSE", caught.exception.code)

    def test_measurement_method_json_shapes_are_typed_rejections(self):
        temporary, config, responses, bodies = self._fixture()
        self.addCleanup(temporary.cleanup)
        url = f"{ENDPOINT}/.well-known/szl-source.json?refresh=1&nonce=unit-test-nonce"
        for malformed in ([], {}, ["SPACE_REPOSITORY_COMMIT"], {"method": "HUGGINGFACE_API"}, 42, None, True):
            with self.subTest(value=malformed):
                responses[url]["deployment"]["measurement_method"] = malformed
                with self.assertRaises(drift.VerificationFailure) as caught:
                    drift.verify_once(
                        config, FakeClient(responses, bodies),
                        drift.new_evidence(config), lambda: 2.0,
                    )
                self.assertEqual("RUNTIME_ATTESTATION_REJECTED", caught.exception.code)

    def test_complete_binding_document_and_canonical_bytes_are_required(self):
        temporary, config, responses, bodies = self._fixture()
        self.addCleanup(temporary.cleanup)
        binding = deploy._source_binding(drift.SOURCE_REPOSITORY, config["source_revision"])
        canonical = deploy._binding_bytes(binding)
        url = drift._resolve_url(REPO_ID, HF_SHA, drift.SOURCE_BINDING_FILENAME)
        cases = {}
        for field in binding:
            missing = dict(binding)
            missing.pop(field)
            cases[f"missing_{field}"] = (json.dumps(missing, indent=2, sort_keys=True) + "\n").encode()
            changed = {**binding, field: "CONFLICTING_VALUE"}
            cases[f"conflicting_{field}"] = (json.dumps(changed, indent=2, sort_keys=True) + "\n").encode()
        cases["additional_field"] = (json.dumps({**binding, "unexpected_claim": "verified"}, indent=2, sort_keys=True) + "\n").encode()
        cases["conflicting_duplicate_field"] = ('{"source_revision":"' + "d" * 40 + '",' + json.dumps(binding)[1:]).encode()
        cases["compact_encoding"] = json.dumps(binding).encode()
        cases["missing_final_lf"] = canonical[:-1]
        cases["crlf_encoding"] = canonical.replace(b"\n", b"\r\n")
        cases["different_field_order"] = (json.dumps(binding, indent=2) + "\n").encode()
        for name, body in cases.items():
            with self.subTest(case=name):
                bodies[url] = body
                with self.assertRaises(drift.VerificationFailure) as caught:
                    drift.verify_once(
                        config, FakeClient(responses, bodies),
                        drift.new_evidence(config), lambda: 2.0,
                    )
                self.assertEqual("SOURCE_BINDING_MISMATCH", caught.exception.code)

    def test_stale_hf_revision_in_runtime_attestation_is_rejected(self):
        temporary, config, json_responses, byte_responses = self._fixture()
        self.addCleanup(temporary.cleanup)
        attestation_url = (
            f"{ENDPOINT}/.well-known/szl-source.json?refresh=1&nonce=unit-test-nonce"
        )
        json_responses[attestation_url]["deployment"]["hf_revision"] = OTHER_HF_SHA
        with self.assertRaises(drift.VerificationFailure) as caught:
            drift.verify_once(
                config,
                FakeClient(json_responses, byte_responses),
                drift.new_evidence(config),
                lambda: 2.0,
            )
        self.assertEqual("STALE_RUNTIME_REVISION", caught.exception.code)

    def test_running_stale_runtime_revision_is_retryable_rejection(self):
        temporary, config, json_responses, byte_responses = self._fixture()
        self.addCleanup(temporary.cleanup)
        json_responses[drift._provider_runtime_url(REPO_ID)] = [
            {"stage": "RUNNING", "sha": OTHER_HF_SHA}
        ]
        with self.assertRaises(drift.VerificationFailure) as caught:
            drift.verify_once(
                config,
                FakeClient(json_responses, byte_responses),
                drift.new_evidence(config),
                lambda: 2.0,
            )
        self.assertEqual("PROVIDER_RUNTIME_REVISION_MISMATCH", caught.exception.code)
        self.assertTrue(caught.exception.retryable)

    def test_unexpected_remote_file_rejects_exact_output_set(self):
        temporary, config, json_responses, byte_responses = self._fixture()
        self.addCleanup(temporary.cleanup)
        json_responses[drift._tree_url(REPO_ID, HF_SHA)].append(
            {"type": "file", "path": "unexpected-startup.py"}
        )
        with self.assertRaises(drift.VerificationFailure) as caught:
            drift.verify_once(
                config,
                FakeClient(json_responses, byte_responses),
                drift.new_evidence(config),
                lambda: 2.0,
            )
        self.assertEqual("PUBLICATION_OUTPUT_SET_MISMATCH", caught.exception.code)

    def test_stale_attestation_timestamp_is_retryable_rejection(self):
        temporary, config, json_responses, byte_responses = self._fixture()
        self.addCleanup(temporary.cleanup)
        url = f"{ENDPOINT}/.well-known/szl-source.json?refresh=1&nonce=unit-test-nonce"
        json_responses[url]["observed_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=10)
        ).isoformat().replace("+00:00", "Z")
        with self.assertRaises(drift.VerificationFailure) as caught:
            drift.verify_once(
                config,
                FakeClient(json_responses, byte_responses),
                drift.new_evidence(config),
                lambda: 2.0,
            )
        self.assertEqual("WITNESS_FRESHNESS_REJECTED", caught.exception.code)
        self.assertTrue(caught.exception.retryable)

    def test_stale_source_binding_is_rejected_even_when_bytes_match(self):
        temporary, config, json_responses, byte_responses = self._fixture()
        self.addCleanup(temporary.cleanup)
        binding_url = drift._resolve_url(
            REPO_ID, HF_SHA, drift.SOURCE_BINDING_FILENAME
        )
        binding = json.loads(byte_responses[binding_url])
        binding["source_revision"] = "d" * 40
        byte_responses[binding_url] = json.dumps(binding).encode()
        with self.assertRaises(drift.VerificationFailure) as caught:
            drift.verify_once(
                config,
                FakeClient(json_responses, byte_responses),
                drift.new_evidence(config),
                lambda: 2.0,
            )
        self.assertEqual("SOURCE_BINDING_MISMATCH", caught.exception.code)

    def test_non_200_readiness_is_rejected(self):
        temporary, config, json_responses, byte_responses = self._fixture()
        self.addCleanup(temporary.cleanup)
        json_responses[f"{ENDPOINT}/readyz?nonce=unit-test-nonce"] = drift.VerificationFailure(
            "HTTP_STATUS",
            "HTTP request returned status 503, expected 200.",
            retryable=True,
            details={"status": 503},
        )
        with self.assertRaises(drift.VerificationFailure) as caught:
            drift.verify_once(
                config,
                FakeClient(json_responses, byte_responses),
                drift.new_evidence(config),
                lambda: 2.0,
            )
        self.assertEqual("HTTP_STATUS", caught.exception.code)

    def test_missing_token_fails_before_network_or_git(self):
        args = argparse.Namespace(
            repo_id=REPO_ID,
            endpoint=ENDPOINT,
            source_revision=SOURCE_SHA,
            token="",
            witness_timeout_seconds="30",
            poll_interval_seconds="1",
            request_timeout_seconds="2",
            source_root=".",
            space_dir="space",
        )
        with self.assertRaises(drift.VerificationFailure) as caught:
            drift.validate_config(args)
        self.assertEqual("MISSING_CREDENTIALS", caught.exception.code)


class _ResponsePolicyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/login")
            self.end_headers()
            return
        if self.path == "/error":
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"unavailable"}')
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html>authentication required</html>")

    def log_message(self, format, *args):
        del format, args


class HttpWitnessPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _ResponsePolicyHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def test_redirect_to_auth_page_is_not_followed(self):
        with self.assertRaises(drift.VerificationFailure) as caught:
            drift.HttpClient("hf_test").get_json(
                f"{self.base}/redirect", timeout=2, authenticate=False
            )
        self.assertEqual("HTTP_REDIRECT", caught.exception.code)

    def test_error_status_and_html_auth_page_are_rejected(self):
        client = drift.HttpClient("hf_test")
        with self.assertRaises(drift.VerificationFailure) as non_200:
            client.get_json(f"{self.base}/error", timeout=2, authenticate=False)
        self.assertEqual("HTTP_STATUS", non_200.exception.code)
        with self.assertRaises(drift.VerificationFailure) as html:
            client.get_json(f"{self.base}/login", timeout=2, authenticate=False)
        self.assertEqual("MALFORMED_HTTP_RESPONSE", html.exception.code)


class WorkflowWiringTests(unittest.TestCase):
    def test_completed_publication_name_and_source_selection_match(self):
        root = Path(__file__).resolve().parent / ".github/workflows"
        publisher = (root / "hf-space-deploy.yml").read_text(encoding="utf-8")
        verifier = (root / "hf-space-drift-check.yml").read_text(encoding="utf-8")
        name = re.search(r"^name: (.+)$", publisher, re.MULTILINE).group(1)
        self.assertIn(f"workflows: [{name}]", verifier)
        paths_block = publisher.split("    paths:\n", 1)[1].split("\npermissions:", 1)[0]
        published_paths = set(re.findall(r'^\s+- "([^"]+)"', paths_block, re.MULTILINE))
        published_paths = {path.removesuffix("/**") for path in published_paths}
        selection = verifier.split("git log -1 --format=%H --", 1)[1].split(')"', 1)[0]
        selected_paths = set(selection.replace("\\", "").split())
        self.assertEqual(published_paths, selected_paths)

    def test_preflight_compiles_in_memory_without_import_bytecode(self):
        root = Path(__file__).resolve().parent / ".github/workflows"
        publisher = (root / "hf-space-deploy.yml").read_text(encoding="utf-8")
        verifier = (root / "hf-space-drift-check.yml").read_text(encoding="utf-8")
        for workflow in (publisher, verifier):
            self.assertIn('PYTHONDONTWRITEBYTECODE: "1"', workflow)
        self.assertNotIn("python -m py_compile", publisher)
        self.assertIn('compile(Path(filename).read_bytes(), filename, "exec")', publisher)

    def test_both_workflows_supply_credentials_config_and_preserve_evidence(self):
        root = Path(__file__).resolve().parent
        for name in ("hf-space-deploy.yml", "hf-space-drift-check.yml"):
            text = (root / ".github" / "workflows" / name).read_text(encoding="utf-8")
            with self.subTest(workflow=name):
                self.assertIn("HF_TOKEN: ${{ secrets.HF_TOKEN }}", text)
                self.assertIn("--repo-id", text)
                self.assertIn("--endpoint", text)
                self.assertIn("--source-revision", text)
                self.assertIn("--evidence-file", text)
                self.assertIn("if: ${{ always() }}", text)
                self.assertIn(
                    "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
                    text,
                )

    def test_production_deploy_has_no_branch_selectable_manual_dispatch(self):
        root = Path(__file__).resolve().parent
        deploy = (root / ".github" / "workflows" / "hf-space-deploy.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("workflow_dispatch:", deploy)

    def test_failed_upstream_deploy_is_explicit_verifier_input(self):
        root = Path(__file__).resolve().parent
        drift_workflow = (
            root / ".github" / "workflows" / "hf-space-drift-check.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("--upstream-conclusion", drift_workflow)
        self.assertNotIn("workflow_run.conclusion == 'success'", drift_workflow)


if __name__ == "__main__":
    unittest.main()
