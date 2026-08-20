#!/usr/bin/env python3
"""Whole-context deploy of the public status Space (space/) to its HF Space.

The llm-router-live Space is a hand-built STATIC status surface (index.html +
assets/, served by `python -m http.server`). It is NOT the router gateway image
that publish.yml ships to GHCR — that image is private, needs provider keys, and
serves a different surface on :8000. So the honest source-of-truth for the Space
is this `space/` directory, deployed whole-context via the HF upload API. The
per-file-COPY org deployer deliberately skips `COPY . /app`, hence this variant.

README front-matter MUST keep `sdk: docker` + `app_port: 7860` — validated below.
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

_SHA = re.compile(r"^[0-9a-f]{40}$")


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
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        sys.exit("README.md front-matter is unterminated — refusing to deploy.")

    values: dict[str, str] = {}
    for line in lines[1:end]:
        if not line or line[0].isspace() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_-]*):[ \t]*(.*)", line)
        if not match or match.group(1) not in {"sdk", "app_port"}:
            continue
        key = match.group(1)
        if key in values:
            sys.exit(f"README front-matter repeats `{key}` — refusing to deploy.")
        value = _front_matter_scalar(match.group(2))
        if value is None:
            sys.exit(f"README front-matter has an invalid `{key}` scalar — refusing to deploy.")
        values[key] = value

    if values.get("sdk") != "docker":
        sys.exit("README front-matter must set top-level `sdk: docker` — refusing to deploy.")
    if values.get("app_port") != "7860":
        sys.exit("README front-matter must set top-level `app_port: 7860` — refusing to deploy.")


def _validate_source_tree(space_dir: Path) -> None:
    for path in space_dir.rglob("*"):
        if path.is_symlink():
            relative = path.relative_to(space_dir).as_posix()
            sys.exit(f"Symbolic links are not deployable: {relative}")


def _source_binding(repository: str, revision: str) -> dict[str, object]:
    repository = repository.strip()
    revision = revision.strip().lower()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        sys.exit("Source repository must be an owner/name identifier.")
    if not _SHA.fullmatch(revision):
        sys.exit("Source revision must be an exact 40-character Git SHA.")
    return {
        "schema": "szl.source-binding/v1",
        "source_repository": repository,
        "source_revision": revision,
        "source_path": "space",
        "relation": "exact-deployed-subtree",
        "evidence_url": f"https://github.com/{repository}/tree/{revision}/space",
    }


def main() -> None:
    from huggingface_hub import HfApi

    ap = argparse.ArgumentParser()
    ap.add_argument("--space-dir", default="space")
    ap.add_argument("--repo-id", default="SZLHOLDINGS/llm-router-live")
    ap.add_argument("--token", required=True)
    ap.add_argument("--commit-message", default="deploy: sync status Space from szl-router/space")
    ap.add_argument(
        "--source-repository",
        default=os.environ.get("GITHUB_REPOSITORY", "szl-holdings/szl-router"),
    )
    ap.add_argument("--source-revision", default=os.environ.get("GITHUB_SHA", ""))
    args = ap.parse_args()

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
    remote_revision = str(
        getattr(api.repo_info(repo_id=args.repo_id, repo_type="space"), "sha", "")
    ).strip().lower()
    if not _SHA.fullmatch(remote_revision):
        sys.exit("Unable to resolve an exact current Space revision — refusing to deploy.")
    with tempfile.TemporaryDirectory(prefix="szl-router-space-") as temporary:
        release_dir = Path(temporary) / "space"
        shutil.copytree(space_dir, release_dir, symlinks=True)
        _validate_source_tree(release_dir)
        (release_dir / "SOURCE_BINDING.json").write_text(
            json.dumps(binding, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        commit = api.upload_folder(
            repo_id=args.repo_id,
            repo_type="space",
            folder_path=str(release_dir),
            commit_message=args.commit_message,
            delete_patterns="*",
            parent_commit=remote_revision,
        )
    published_revision = str(getattr(commit, "oid", "")).strip().lower()
    if not _SHA.fullmatch(published_revision):
        sys.exit("Provider upload did not return an exact immutable revision.")
    print(
        f"Deployed {space_dir} -> space/{args.repo_id}@{published_revision} "
        f"from parent {remote_revision}"
    )


if __name__ == "__main__":
    main()
