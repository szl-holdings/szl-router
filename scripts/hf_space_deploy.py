#!/usr/bin/env python3
"""Publish the source-bound SZL Router flagship Space.

The public Space is generated only from ``szl-router/space``. The private or
operator-hosted OpenAI-compatible gateway remains a separate deployment target.
This publisher never creates a second Space and never uploads router credentials,
provider topology, or gateway runtime configuration.

Publication order is deliberate:

1. resolve the existing Space and its immutable parent revision;
2. upload the exact ``space/`` subtree plus ``SOURCE_BINDING.json``;
3. restore the existing Space to public visibility;
4. restart the Docker Space so the new immutable revision is served; and
5. verify public visibility and exact provider revision before returning.

The read-only drift verifier performs the deeper file, runtime, endpoint, and
source-attestation checks after this publisher exits.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

_SHA = re.compile(r"^[0-9a-f]{40}$")
_REPO_ID = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _front_matter_scalar(raw: str) -> str | None:
    for pattern in (
        r'"([^"\r\n]*)"[ \t]*(?:#.*)?',
        r"'([^'\r\n]*)'[ \t]*(?:#.*)?",
        r"([^#]*?)[ \t]*(?:#.*)?",
    ):
        match = re.fullmatch(pattern, raw)
        if match:
            return match.group(1).strip()
    return None


def _validate_readme(space_dir: Path) -> None:
    readme = space_dir / "README.md"
    text = readme.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        sys.exit("README.md has no YAML front-matter — refusing to deploy.")
    try:
        end = next(
            index
            for index, line in enumerate(lines[1:], 1)
            if line.strip() == "---"
        )
    except StopIteration:
        sys.exit("README.md front-matter is unterminated — refusing to deploy.")

    values: dict[str, str] = {}
    required = {"sdk", "app_port", "pinned", "license"}
    for line in lines[1:end]:
        if not line or line[0].isspace() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_-]*):[ \t]*(.*)", line)
        if not match or match.group(1) not in required:
            continue
        key = match.group(1)
        if key in values:
            sys.exit(f"README front-matter repeats `{key}` — refusing to deploy.")
        value = _front_matter_scalar(match.group(2))
        if value is None:
            sys.exit(
                f"README front-matter has an invalid `{key}` scalar — refusing to deploy."
            )
        values[key] = value

    expected = {
        "sdk": "docker",
        "app_port": "7860",
        "pinned": "true",
        "license": "apache-2.0",
    }
    mismatch = {
        key: {"expected": expected_value, "observed": values.get(key)}
        for key, expected_value in expected.items()
        if values.get(key) != expected_value
    }
    if mismatch:
        sys.exit(
            "README front-matter does not describe the router flagship: "
            + json.dumps(mismatch, sort_keys=True)
        )


def _validate_source_tree(space_dir: Path) -> None:
    for path in space_dir.rglob("*"):
        if path.is_symlink():
            relative = path.relative_to(space_dir).as_posix()
            sys.exit(f"Symbolic links are not deployable: {relative}")


def _source_binding(repository: str, revision: str) -> dict[str, object]:
    repository = repository.strip()
    revision = revision.strip().lower()
    if not _REPO_ID.fullmatch(repository):
        sys.exit("Source repository must be an owner/name identifier.")
    if not _SHA.fullmatch(revision):
        sys.exit("Source revision must be an exact 40-character Git SHA.")
    return {
        "schema": "szl.source-binding/v1",
        "source_repository": repository,
        "source_revision": revision,
        "source_path": "space",
        "relation": "exact-deployed-subtree",
        "product_class": "FLAGSHIP_LLM_GATEWAY",
        "public_space": "SZLHOLDINGS/llm-router-live",
        "evidence_url": f"https://github.com/{repository}/tree/{revision}/space",
    }


def _exact_revision(value: Any, *, field: str) -> str:
    revision = str(value or "").strip().lower()
    if not _SHA.fullmatch(revision):
        sys.exit(f"{field} is not an exact immutable revision.")
    return revision


def _restore_public_visibility(api: Any, repo_id: str) -> str:
    """Set only the existing Space's visibility; never create or rename it."""

    update_settings = getattr(api, "update_repo_settings", None)
    if callable(update_settings):
        update_settings(repo_id=repo_id, repo_type="space", private=False)
        method = "update_repo_settings"
    else:
        update_visibility = getattr(api, "update_repo_visibility", None)
        if not callable(update_visibility):
            sys.exit(
                "Installed huggingface_hub cannot update Space visibility — refusing "
                "to claim flagship restoration."
            )
        update_visibility(repo_id=repo_id, repo_type="space", private=False)
        method = "update_repo_visibility"

    info = api.repo_info(repo_id=repo_id, repo_type="space")
    if bool(getattr(info, "private", True)):
        sys.exit("Provider readback still reports the router Space as private.")
    return method


def _restart_existing_space(api: Any, repo_id: str) -> None:
    restart = getattr(api, "restart_space", None)
    if not callable(restart):
        sys.exit(
            "Installed huggingface_hub cannot restart the existing Space — refusing "
            "to claim runtime restoration."
        )
    restart(repo_id=repo_id)


def main() -> None:
    from huggingface_hub import HfApi

    parser = argparse.ArgumentParser()
    parser.add_argument("--space-dir", default="space")
    parser.add_argument("--repo-id", default="SZLHOLDINGS/llm-router-live")
    parser.add_argument("--token", required=True)
    parser.add_argument(
        "--commit-message",
        default="deploy: restore SZL Router flagship from exact GitHub source",
    )
    parser.add_argument(
        "--source-repository",
        default=os.environ.get("GITHUB_REPOSITORY", "szl-holdings/szl-router"),
    )
    parser.add_argument(
        "--source-revision",
        default=os.environ.get("GITHUB_SHA", ""),
    )
    args = parser.parse_args()

    repo_id = args.repo_id.strip()
    if repo_id != "SZLHOLDINGS/llm-router-live":
        sys.exit(
            "This publisher is single-target and may only restore "
            "SZLHOLDINGS/llm-router-live."
        )

    requested_space_dir = Path(args.space_dir)
    if requested_space_dir.is_symlink():
        sys.exit("The Space source root cannot be a symbolic link.")
    space_dir = requested_space_dir.resolve()
    if not space_dir.is_dir():
        sys.exit(f"{space_dir} is not a directory.")
    _validate_readme(space_dir)
    _validate_source_tree(space_dir)

    binding = _source_binding(args.source_repository, args.source_revision)
    api = HfApi(token=args.token)

    # Resolve only the existing target. A missing target is a hard failure: this
    # workflow does not mint a second router Space.
    before = api.repo_info(repo_id=repo_id, repo_type="space")
    parent_revision = _exact_revision(
        getattr(before, "sha", ""),
        field="Current Space revision",
    )

    with tempfile.TemporaryDirectory(prefix="szl-router-space-") as temporary:
        release_dir = Path(temporary) / "space"
        shutil.copytree(space_dir, release_dir, symlinks=True)
        _validate_source_tree(release_dir)
        (release_dir / "SOURCE_BINDING.json").write_text(
            json.dumps(binding, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        commit = api.upload_folder(
            repo_id=repo_id,
            repo_type="space",
            folder_path=str(release_dir),
            commit_message=args.commit_message,
            delete_patterns="*",
            parent_commit=parent_revision,
        )

    published_revision = _exact_revision(
        getattr(commit, "oid", ""),
        field="Published Space revision",
    )
    visibility_method = _restore_public_visibility(api, repo_id)
    _restart_existing_space(api, repo_id)

    after = api.repo_info(repo_id=repo_id, repo_type="space")
    observed_revision = _exact_revision(
        getattr(after, "sha", ""),
        field="Post-restoration Space revision",
    )
    if observed_revision != published_revision:
        sys.exit(
            "Provider revision changed during restoration: "
            f"published={published_revision}, observed={observed_revision}."
        )
    if bool(getattr(after, "private", True)):
        sys.exit("Post-restoration provider readback reports private=true.")

    print(
        json.dumps(
            {
                "schema": "szl.router-flagship-publication/v1",
                "repo_id": repo_id,
                "source_repository": binding["source_repository"],
                "source_revision": binding["source_revision"],
                "parent_revision": parent_revision,
                "published_revision": published_revision,
                "observed_revision": observed_revision,
                "private": False,
                "visibility_method": visibility_method,
                "restart_requested": True,
                "space_created": False,
                "credential_value_recorded": False,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
