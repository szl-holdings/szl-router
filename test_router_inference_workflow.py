"""Offline checks for artifact-only inference preparation authority."""
from pathlib import Path
import re
import unittest

import yaml


WORKFLOW = Path(__file__).parent / ".github/workflows/prepare-router-inference.yml"


class InferenceWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")
        self.workflow = yaml.load(self.text, Loader=yaml.BaseLoader)
        self.prepare = self.workflow["jobs"]["prepare"]
        self.steps = self.prepare["steps"]

    def test_preparation_is_owner_dispatch_on_main_only(self):
        condition = self.prepare["if"]
        for guard in (
            "github.event_name == 'workflow_dispatch'",
            "github.repository == 'szl-holdings/szl-router'",
            "github.ref == 'refs/heads/main'",
        ):
            self.assertIn(guard, condition)
        self.assertEqual(self.prepare["needs"], "contract")
        self.assertEqual(self.workflow["concurrency"]["cancel-in-progress"], "false")

    def test_no_cloud_or_write_credentials(self):
        self.assertEqual(self.workflow["permissions"], {})
        self.assertEqual(self.prepare["permissions"], {
            "contents": "read", "packages": "read", "attestations": "read",
        })
        self.assertEqual(self.workflow["jobs"]["contract"]["permissions"], {"contents": "read"})
        self.assertEqual(set(re.findall(r"secrets\.([A-Za-z0-9_]+)", self.text)), {"GITHUB_TOKEN"})
        for forbidden in ("HF_TOKEN", "OPENAI_API_KEY", "SZL_ROUTER_TOKEN", "id-token:",
                          "router_control_space.py", "huggingface_hub", "docker push", "cosign sign"):
            self.assertNotIn(forbidden, self.text)

    def test_exact_image_is_verified_before_build(self):
        names = [step["name"] for step in self.steps]
        verify = next(step["run"] for step in self.steps if step["name"] == "Verify exact image signature and provenance")
        self.assertIn("publish-router-control.yml@refs/heads/main", verify)
        self.assertIn('--source-digest "$GITHUB_SHA"', verify)
        self.assertIn("--source-ref refs/heads/main", verify)
        self.assertIn("--deny-self-hosted-runners", verify)
        self.assertLess(names.index("Verify exact image signature and provenance"),
                        names.index("Prepare immutable disabled bundle"))
        self.assertLess(names.index("Prepare immutable disabled bundle"),
                        names.index("Exercise staged image without network access"))
        self.assertEqual(self.text.count('git ls-remote https://github.com/szl-holdings/szl-router.git refs/heads/main'), 2)

    def test_actions_are_sha_pinned_and_checkouts_do_not_persist_credentials(self):
        for job in self.workflow["jobs"].values():
            for step in job["steps"]:
                if "uses" in step:
                    self.assertRegex(step["uses"], r"^[^@\s]+@[0-9a-f]{40}$")
                if step.get("uses", "").startswith("actions/checkout@"):
                    self.assertEqual(step["with"]["persist-credentials"], "false")

    def test_inputs_are_environment_data_not_shell_interpolation(self):
        self.assertEqual(self.prepare["env"]["IMAGE_DIGEST"], "${{ inputs.image_digest }}")
        for step in self.steps:
            self.assertNotIn("${{ inputs.", step.get("run", ""))
        admit = next(step["run"] for step in self.steps if step["name"] == "Admit source and immutable image")
        self.assertIn('[[ "$IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]', admit)

    def test_failed_evidence_retained_without_claiming_success(self):
        artifact = self.steps[-1]
        self.assertEqual(artifact["if"], "${{ always() }}")
        self.assertEqual(artifact["with"]["path"], "${{ runner.temp }}/inference-evidence/")
        self.assertNotIn("continue-on-error", self.text)

    def test_contract_runs_in_pull_requests(self):
        for trigger in ("pull_request", "push"):
            self.assertEqual(self.workflow["on"][trigger]["branches"], ["main"])
            for path in ("scripts/router_inference_bundle.py", "test_router_inference_bundle.py",
                         "test_router_inference_workflow.py", ".github/workflows/prepare-router-inference.yml"):
                self.assertIn(path, self.workflow["on"][trigger]["paths"])
        contract = self.workflow["jobs"]["contract"]
        self.assertTrue(any("test_router_inference_workflow" in step.get("run", "") for step in contract["steps"]))


if __name__ == "__main__":
    unittest.main()
