import tempfile
import unittest
from pathlib import Path

from scripts import hf_space_deploy as deploy


class DeployAdmissionTests(unittest.TestCase):
    def _space(self, front_matter: str) -> tuple[tempfile.TemporaryDirectory, Path]:
        temporary = tempfile.TemporaryDirectory()
        space = Path(temporary.name) / "space"
        space.mkdir()
        (space / "README.md").write_text(front_matter, encoding="utf-8")
        return temporary, space

    def test_exact_top_level_front_matter_is_accepted(self):
        temporary, space = self._space(
            "---\nsdk: docker # required runtime\napp_port: '7860'\n---\n"
        )
        self.addCleanup(temporary.cleanup)
        deploy._validate_readme(space)

    def test_comment_cannot_spoof_sdk_or_port(self):
        temporary, space = self._space(
            "---\nsdk: static\n# sdk: docker\napp_port: 80 # app_port: 7860\n---\n"
        )
        self.addCleanup(temporary.cleanup)
        with self.assertRaises(SystemExit):
            deploy._validate_readme(space)

    def test_duplicate_runtime_keys_are_rejected(self):
        temporary, space = self._space(
            "---\nsdk: docker\nsdk: static\napp_port: 7860\n---\n"
        )
        self.addCleanup(temporary.cleanup)
        with self.assertRaises(SystemExit):
            deploy._validate_readme(space)

    def test_directory_symlink_is_rejected_when_supported(self):
        temporary, space = self._space("---\nsdk: docker\napp_port: 7860\n---\n")
        self.addCleanup(temporary.cleanup)
        outside = Path(temporary.name) / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("not deployable", encoding="utf-8")
        link = space / "linked-directory"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Directory symlinks are unavailable: {type(exc).__name__}")
        with self.assertRaises(SystemExit):
            deploy._validate_source_tree(space)


if __name__ == "__main__":
    unittest.main()
