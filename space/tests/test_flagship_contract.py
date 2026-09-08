from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

SPACE = Path(__file__).resolve().parents[1]
ROOT = SPACE.parent
README = SPACE / "README.md"
INDEX = SPACE / "index.html"
BOOTSTRAP = ROOT / "scripts" / "hf_space_bootstrap.py"
DEPLOY = ROOT / "scripts" / "hf_space_deploy.py"
WORKFLOW = ROOT / ".github" / "workflows" / "hf-space-deploy.yml"
HF_CARD_COLORS = {"red", "yellow", "green", "blue", "indigo", "purple", "pink", "gray"}


class RouterFlagshipContractTests(unittest.TestCase):
    def test_space_card_declares_one_public_flagship(self) -> None:
        text = README.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        front_matter = text.split("---", 2)[1]
        expected = {
            "title": "SZL Router — Sovereign LLM Gateway",
            "colorFrom": "blue",
            "colorTo": "indigo",
            "sdk": "docker",
            "app_port": "7860",
            "pinned": "true",
            "license": "apache-2.0",
            "short_description": "Sovereign LLM routing with per-answer receipts.",
        }
        observed = {}
        for line in front_matter.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            observed[key.strip()] = value.strip()
        for key, value in expected.items():
            self.assertEqual(value, observed.get(key), key)
        self.assertIn(observed["colorFrom"], HF_CARD_COLORS)
        self.assertIn(observed["colorTo"], HF_CARD_COLORS)
        self.assertLessEqual(len(observed["short_description"]), 60)

        self.assertIn("# SZL Router — flagship LLM gateway", text)
        self.assertIn("https://github.com/szl-holdings/szl-router", text)
        self.assertIn("https://a-11-oy.com/code", text)
        self.assertIn("SZLHOLDINGS/llm-router-live", text)
        self.assertNotIn("codebase and its routing logic stay **private**", text)

    def test_frontend_is_distinct_accessible_and_source_linked(self) -> None:
        text = INDEX.read_text(encoding="utf-8")
        lowered = text.lower()
        required = (
            'data-szl-surface="router-flagship"',
            "FLAGSHIP · LLM GATEWAY",
            "One gateway.",
            "Holographic model-routing topology",
            "source of truth",
            "SZLHOLDINGS/llm-router-live",
            "https://github.com/szl-holdings/szl-router",
            "https://a-11-oy.com/code",
            'href="#main"',
            "prefers-reduced-motion:reduce",
            "forced-colors:active",
            "min-height:44px",
            'id="source-pill"',
            'id="data-source-note"',
            'id="model-chips"',
            'id="provider-grid"',
            'id="updated-stamp"',
        )
        for marker in required:
            self.assertIn(marker, text)

        self.assertNotIn("router internals stay private", lowered)
        self.assertNotIn("codebase and its routing logic stay private", lowered)
        self.assertNotRegex(lowered, r"https?://(?:10|127|169\.254|192\.168)\.")

    def test_publisher_and_bootstrap_are_single_target(self) -> None:
        source = DEPLOY.read_text(encoding="utf-8")
        ast.parse(source)
        required = (
            'repo_id != "SZLHOLDINGS/llm-router-live"',
            'api.repo_info(repo_id=repo_id, repo_type="space")',
            'private=False',
            'restart(repo_id=repo_id)',
            '"space_created": False',
            '"credential_value_recorded": False',
            '"product_class": "FLAGSHIP_LLM_GATEWAY"',
        )
        for marker in required:
            self.assertIn(marker, source)
        self.assertNotIn("create_repo", source)
        self.assertNotIn("delete_repo", source)
        self.assertNotIn("delete_space", source)

        bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
        ast.parse(bootstrap)
        for marker in (
            'TARGET_REPO_ID = "SZLHOLDINGS/llm-router-live"',
            'ref != "refs/heads/main"',
            'api.create_repo(',
            'repo_id=TARGET_REPO_ID',
            'repo_type="space"',
            'private=False',
            'exist_ok=True',
            'space_sdk="docker"',
            '"exact_target_only": True',
            '"hardware_changed": False',
            '"credential_value_recorded": False',
        ):
            self.assertIn(marker, bootstrap)
        self.assertNotIn("delete_repo", bootstrap)
        self.assertNotIn("delete_space", bootstrap)

    def test_workflow_uses_exact_source_and_terminal_readback(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        required = (
            "Deploy SZL Router flagship Space",
            "SZLHOLDINGS/llm-router-live",
            "https://szlholdings-llm-router-live.hf.space",
            "ref: ${{ github.sha }}",
            'test "$(git rev-parse HEAD)" = "$GITHUB_SHA"',
            'huggingface_hub==1.10.1',
            "scripts/hf_space_bootstrap.py",
            "scripts/hf_space_deploy.py",
            "scripts/hf_space_drift_check.py",
            "router-space-bootstrap.json",
            "witness-timeout-seconds 900",
            "environment: production",
        )
        for marker in required:
            self.assertIn(marker, workflow)
        self.assertNotIn("workflow_dispatch", workflow)

        uses = re.findall(
            r"^\s*uses:\s*(\S+)\s*(?:#.*)?$",
            workflow,
            re.MULTILINE,
        )
        self.assertTrue(uses)
        self.assertFalse(
            [value for value in uses if not re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", value)]
        )


if __name__ == "__main__":
    unittest.main()
