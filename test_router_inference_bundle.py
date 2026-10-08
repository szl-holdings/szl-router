"""Offline tests for prepare-only, disabled inference staging artifacts."""
from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import router_inference_bundle as bundle
from router_control.app import Registry, inference_readiness, load_settings

SOURCE = "a" * 40
IMAGE = bundle.IMAGE + "@sha256:" + "b" * 64


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="router-bundle-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.output = self.root / "new-bundle"

    def prepare(self):
        with patch.object(bundle, "require_source") as source:
            result = bundle.prepare_bundle(SOURCE, IMAGE, self.output)
            self.assertEqual(source.call_count, 2)
            return result

    def test_actual_registry_schema_is_fixed_disabled_and_public_only(self):
        value = bundle.disabled_registry()
        parsed = Registry.model_validate(value)
        self.assertEqual(len(parsed.providers), 1)
        provider = parsed.providers[0]
        self.assertEqual(provider.base_url, "https://api.openai.com/v1")
        self.assertEqual(provider.models, {"szl-astra": "gpt-6-astra"})
        self.assertFalse(provider.enabled)
        self.assertEqual(provider.classifications, ["public"])
        self.assertEqual(provider.sovereignty, 0)

    def test_deterministic_bytes_do_not_copy_environment_secrets(self):
        first = bundle.bundle_files(SOURCE, IMAGE)
        with patch.dict(os.environ, {"HF_TOKEN": "PRIVATE_HF_FIXTURE", "OPENAI_API_KEY": "PRIVATE_API_FIXTURE",
                                     "SZL_ROUTER_TOKEN": "PRIVATE_CALLER_FIXTURE", "SZL_ROUTER_OPENAI_TOKEN": "PRIVATE_UPSTREAM_FIXTURE"}):
            second = bundle.bundle_files(SOURCE, IMAGE)
        self.assertEqual(first, second)
        self.assertFalse(any(b"PRIVATE_" in value for value in second.values()))

    def test_image_and_source_claims_stay_unverified(self):
        files = bundle.bundle_files(SOURCE, IMAGE)
        binding = json.loads(files["SOURCE_BINDING.json"])
        for field in ("source_authenticity", "image_authenticity", "image_source_alignment", "remote_main_admission"):
            self.assertEqual(binding[field], "UNKNOWN")
        self.assertIsNone(binding["target"])
        self.assertFalse(binding["deployment_authorized"])
        self.assertEqual(binding["required_secret_names"], list(bundle.SECRET_NAMES))
        self.assertEqual(binding["inference_state"], "UNAVAILABLE")
        self.assertFalse(binding["cost_tier_is_usd_cap"])

    def test_launch_overrides_armed_environment_and_unsets_credentials(self):
        docker = bundle.bundle_files(SOURCE, IMAGE)["Dockerfile"].decode()
        self.assertTrue(docker.startswith("FROM " + IMAGE + "\n"))
        command = json.loads(next(line[4:] for line in docker.splitlines() if line.startswith("CMD ")))
        self.assertEqual(command[:3], ["python", "-I", "-c"])
        hostile = {"SZL_ROUTER_ENABLE_EGRESS": "1", "SZL_ROUTER_ALLOWED_HOSTS": "attacker.invalid",
                   "SZL_ROUTER_PROVIDERS_JSON": '{"providers":[]}', "PORT": "9999",
                   "SZL_ROUTER_TOKEN": "PRIVATE_CALLER", "SZL_ROUTER_OPENAI_TOKEN": "PRIVATE_UPSTREAM"}
        with patch.dict(os.environ, hostile, clear=True), patch.object(os, "execv") as execute:
            exec(command[3], {})
            settings = load_settings()
            self.assertEqual(settings.config_state, "VALIDATED")
            self.assertFalse(settings.egress_enabled)
            self.assertEqual(settings.allowed_hosts, frozenset({"api.openai.com"}))
            self.assertFalse(settings.registry.providers[0].enabled)
            self.assertFalse(inference_readiness(settings)["ready_for_requests"])
            for name in bundle.SECRET_NAMES:
                self.assertNotIn(name, os.environ)
            argv = execute.call_args.args[1]
            self.assertEqual(argv[1:5], ["-I", "-m", "uvicorn", "router_control.app:app"])
            self.assertEqual(argv[argv.index("--app-dir") + 1], "/app")
            self.assertIn("7860", argv)
            self.assertNotIn("9999", argv)

    def test_real_child_interpreter_ignores_python_environment_injection(self):
        docker = bundle.bundle_files(SOURCE, IMAGE)["Dockerfile"].decode()
        command = json.loads(next(line[4:] for line in docker.splitlines() if line.startswith("CMD ")))
        with patch.dict(os.environ, {}, clear=True), patch.object(os, "execv") as execute:
            exec(command[3], {})
            argv = execute.call_args.args[1]
        (self.root / "sitecustomize.py").write_text("raise RuntimeError('inherited module injection')\n")
        environment = dict(os.environ, PYTHONPATH=str(self.root), PYTHONHOME=str(self.root / "missing-home"))
        # Preserve the generated child interpreter's startup flags, replacing only
        # its application module with a no-network introspection command.
        flags = argv[1:argv.index("-m")]
        result = subprocess.run([argv[0], *flags, "-c", "import json,sys;print(json.dumps({'isolated':sys.flags.isolated,'ignore_environment':sys.flags.ignore_environment,'path':sys.path}))"],
                                env=environment, cwd=self.root, capture_output=True, text=True, timeout=10, check=True)
        observed = json.loads(result.stdout)
        self.assertEqual(observed["isolated"], 1)
        self.assertEqual(observed["ignore_environment"], 1)
        self.assertNotIn(str(self.root), observed["path"])

    def test_rejects_invalid_source_or_image_before_git_or_output(self):
        invalid = [("main", IMAGE), (SOURCE.upper(), IMAGE), (SOURCE + "\n", IMAGE),
                   (SOURCE, bundle.IMAGE + ":latest"), (SOURCE, "other.example/router@sha256:" + "b" * 64),
                   (SOURCE, IMAGE + "\nRUN echo bad"), (SOURCE, IMAGE.replace("sha256:", "sha512:"))]
        with patch.object(bundle, "require_source") as source:
            for revision, image in invalid:
                with self.subTest(revision=revision, image=image), self.assertRaises(bundle.BundleError):
                    bundle.prepare_bundle(revision, image, self.output)
            source.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_success_has_complete_hash_manifest_and_no_remote_effect(self):
        with patch("socket.create_connection", side_effect=AssertionError("No network")), \
                patch("urllib.request.urlopen", side_effect=AssertionError("No network")):
            manifest = self.prepare()
        self.assertEqual(set(p.name for p in self.output.iterdir()), set(bundle.bundle_files(SOURCE, IMAGE)) | {bundle.MANIFEST})
        for name, digest in manifest["file_sha256"].items():
            self.assertEqual(hashlib.sha256((self.output / name).read_bytes()).hexdigest(), digest)
        self.assertEqual(manifest["remote_writes"], 0)
        self.assertEqual(manifest["inference_calls"], 0)
        self.assertFalse(manifest["deployment_authorized"])

    def test_existing_nonempty_output_is_preserved(self):
        self.output.mkdir()
        marker = self.output / "prior-evidence.json"
        marker.write_bytes(b"preserve")
        with patch.object(bundle, "require_source"), self.assertRaisesRegex(bundle.BundleError, "OUTPUT_ALREADY_EXISTS"):
            bundle.prepare_bundle(SOURCE, IMAGE, self.output)
        self.assertEqual(marker.read_bytes(), b"preserve")
        self.assertEqual(len(list(self.output.iterdir())), 1)

    def test_existing_empty_directory_is_also_refused(self):
        self.output.mkdir()
        with patch.object(bundle, "require_source"), self.assertRaisesRegex(bundle.BundleError, "OUTPUT_ALREADY_EXISTS"):
            bundle.prepare_bundle(SOURCE, IMAGE, self.output)

    def test_output_parent_must_exist_and_no_traversal(self):
        for output in [self.root / "missing" / "bundle", self.root / ".." / "bundle"]:
            with self.subTest(output=output), patch.object(bundle, "require_source"), self.assertRaises(bundle.BundleError):
                bundle.prepare_bundle(SOURCE, IMAGE, output)

    def test_output_inside_checkout_is_rejected(self):
        with patch.object(bundle, "require_source"), self.assertRaisesRegex(bundle.BundleError, "OUTPUT_INSIDE_SOURCE_REJECTED"):
            bundle.prepare_bundle(SOURCE, IMAGE, bundle.ROOT / "generated-output")

    def test_simulated_symlink_or_reparse_ancestor_rejected(self):
        original = Path.lstat
        for mode, attributes in [(stat.S_IFLNK, 0), (stat.S_IFDIR, getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024))]:
            def lstat(path):
                if path == self.root:
                    return SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
                return original(path)
            with self.subTest(mode=mode), patch.object(Path, "lstat", lstat), patch.object(bundle, "require_source"), self.assertRaises(bundle.BundleError):
                bundle.prepare_bundle(SOURCE, IMAGE, self.output)
        self.assertFalse(self.output.exists())

    def test_existing_file_is_not_overwritten(self):
        self.output.mkdir()
        target = self.output / "Dockerfile"
        target.write_bytes(b"previous")
        info = self.output.stat()
        with self.assertRaisesRegex(bundle.BundleError, "OUTPUT_WRITE_FAILED"):
            bundle._write_new(self.output, (info.st_dev, info.st_ino), "Dockerfile", b"replacement")
        self.assertEqual(target.read_bytes(), b"previous")

    def test_write_failure_leaves_no_completion_manifest(self):
        real = bundle._write_new
        def write(output, identity, name, body):
            if name == "README.md":
                raise bundle.BundleError("OUTPUT_WRITE_FAILED")
            real(output, identity, name, body)
        with patch.object(bundle, "require_source"), patch.object(bundle, "_write_new", side_effect=write), self.assertRaises(bundle.BundleError):
            bundle.prepare_bundle(SOURCE, IMAGE, self.output)
        self.assertTrue((self.output / "Dockerfile").is_file())
        self.assertFalse((self.output / bundle.MANIFEST).exists())

    def test_source_changes_before_completion_refuse_manifest(self):
        with patch.object(bundle, "require_source", side_effect=[None, bundle.BundleError("LOCAL_SOURCE_DIRTY")]), self.assertRaises(bundle.BundleError):
            bundle.prepare_bundle(SOURCE, IMAGE, self.output)
        self.assertFalse((self.output / bundle.MANIFEST).exists())

    def test_output_directory_identity_drift_refused(self):
        self.output.mkdir()
        with self.assertRaisesRegex(bundle.BundleError, "OUTPUT_DIRECTORY_CHANGED"):
            bundle._write_new(self.output, (-1, -1), "Dockerfile", b"data")

    def test_cli_no_extra_url_target_model_or_enable_arguments(self):
        for argument in ["--target", "--url", "--model", "--enable-egress", "--token"]:
            with self.subTest(argument=argument), redirect_stdout(io.StringIO()), patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
                bundle.main(["--source-revision", SOURCE, "--image-reference", IMAGE,
                             "--output", str(self.output), argument, "unused"])

    def test_cli_errors_do_not_echo_exception_or_secret_inputs(self):
        output = io.StringIO()
        with patch.object(bundle, "prepare_bundle", side_effect=ValueError("PRIVATE_ERROR")), redirect_stdout(output):
            code = bundle.main(["--source-revision", SOURCE, "--image-reference", IMAGE, "--output", str(self.output)])
        self.assertEqual(code, 1)
        self.assertNotIn("PRIVATE_ERROR", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["code"], "PREPARATION_FAILED")


class SourceTests(unittest.TestCase):
    def git(self, *args):
        return {("remote", "get-url", "origin"): "https://github.com/szl-holdings/szl-router.git",
                ("rev-parse", "--show-toplevel"): str(bundle.ROOT), ("rev-parse", "HEAD"): SOURCE,
                ("status", "--porcelain=v1", "--untracked-files=all"): ""}[args]

    def test_canonical_origins_and_clean_exact_source(self):
        for origin in bundle.ORIGINS:
            def git(*args):
                return origin if args == ("remote", "get-url", "origin") else self.git(*args)
            with self.subTest(origin=origin), patch.object(bundle, "_git", side_effect=git):
                bundle.require_source(SOURCE)

    def test_wrong_repo_checkout_revision_and_dirty_untracked_refused(self):
        variants = [("remote", "get-url", "origin"), ("rev-parse", "--show-toplevel"),
                    ("rev-parse", "HEAD"), ("status", "--porcelain=v1", "--untracked-files=all")]
        for bad in variants:
            def git(*args):
                return "wrong" if args == bad else self.git(*args)
            with self.subTest(bad=bad), patch.object(bundle, "_git", side_effect=git), self.assertRaises(bundle.BundleError):
                bundle.require_source(SOURCE)

    def test_git_failure_is_sanitized_and_has_no_network_command(self):
        with patch.object(subprocess, "run", side_effect=subprocess.CalledProcessError(1, "git", stderr="PRIVATE")) as run:
            with self.assertRaisesRegex(bundle.BundleError, "LOCAL_SOURCE_UNAVAILABLE"):
                bundle._git("rev-parse", "HEAD")
        self.assertEqual(run.call_args.args[0], ["git", "-C", str(bundle.ROOT), "rev-parse", "HEAD"])


if __name__ == "__main__":
    unittest.main()
