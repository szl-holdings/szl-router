from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "hf_space_bootstrap.py"
SPEC = importlib.util.spec_from_file_location("hf_space_bootstrap", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
bootstrap = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bootstrap
SPEC.loader.exec_module(bootstrap)


class FakeNotFound(Exception):
    pass


class FakeApi:
    def __init__(self, *, missing: bool) -> None:
        self.missing = missing
        self.created: list[dict[str, object]] = []
        self.lookups = 0

    def repo_info(self, *, repo_id: str, repo_type: str):
        self.lookups += 1
        if self.missing:
            self.missing = False
            raise FakeNotFound(repo_id)
        return SimpleNamespace(sha="a" * 40, private=False)

    def create_repo(self, **kwargs):
        self.created.append(kwargs)
        return "https://huggingface.co/spaces/SZLHOLDINGS/llm-router-live"


class RouterSpaceBootstrapTests(unittest.TestCase):
    def test_existing_target_is_read_only(self) -> None:
        api = FakeApi(missing=False)
        result = bootstrap.ensure_canonical_space(
            api,
            bootstrap.TARGET_REPO_ID,
            not_found_error=FakeNotFound,
        )
        self.assertFalse(result["space_created"])
        self.assertEqual([], api.created)
        self.assertEqual(1, api.lookups)
        self.assertEqual("a" * 40, result["observed_revision"])

    def test_missing_target_creates_only_exact_public_docker_space(self) -> None:
        api = FakeApi(missing=True)
        result = bootstrap.ensure_canonical_space(
            api,
            bootstrap.TARGET_REPO_ID,
            not_found_error=FakeNotFound,
        )
        self.assertTrue(result["space_created"])
        self.assertEqual(2, api.lookups)
        self.assertEqual(
            [
                {
                    "repo_id": "SZLHOLDINGS/llm-router-live",
                    "repo_type": "space",
                    "private": False,
                    "exist_ok": True,
                    "space_sdk": "docker",
                }
            ],
            api.created,
        )

    def test_alternate_target_is_rejected_before_provider_access(self) -> None:
        api = FakeApi(missing=False)
        with self.assertRaises(SystemExit):
            bootstrap.ensure_canonical_space(
                api,
                "SZLHOLDINGS/router-copy",
                not_found_error=FakeNotFound,
            )
        self.assertEqual(0, api.lookups)
        self.assertEqual([], api.created)

    def test_bootstrap_requires_exact_protected_main_identity(self) -> None:
        sha = "b" * 40
        self.assertEqual(
            sha,
            bootstrap._require_protected_main("refs/heads/main", sha, sha),
        )
        for ref, workflow_sha, requested in (
            ("refs/heads/feature", sha, sha),
            ("refs/heads/main", "c" * 40, sha),
            ("refs/heads/main", sha, "not-a-sha"),
        ):
            with self.assertRaises(SystemExit):
                bootstrap._require_protected_main(ref, workflow_sha, requested)


if __name__ == "__main__":
    unittest.main()
