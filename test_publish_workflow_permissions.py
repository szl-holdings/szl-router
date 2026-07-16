"""Regression tests for least-privilege package publishing."""

from pathlib import Path


WORKFLOW = Path(__file__).parent / ".github" / "workflows" / "publish.yml"


def _indented_block(lines: list[str], header: str, indent: int) -> list[str]:
    """Return the YAML lines nested directly beneath a uniquely named header."""
    start = lines.index((" " * indent) + header) + 1
    block: list[str] = []
    for line in lines[start:]:
        if line and len(line) - len(line.lstrip()) <= indent:
            break
        block.append(line)
    return block


def test_package_write_is_scoped_to_the_publish_job() -> None:
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()

    workflow_permissions = _indented_block(lines, "permissions:", 0)
    assert "  contents: read" in workflow_permissions
    assert "  packages: write" not in workflow_permissions

    publish_job = _indented_block(lines, "publish:", 2)
    assert "    permissions:" in publish_job
    assert "      contents: read" in publish_job
    assert "      packages: write" in publish_job

    assert lines.count("      packages: write") == 1
