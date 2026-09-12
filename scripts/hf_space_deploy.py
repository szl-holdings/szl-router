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
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

_SHA = re.compile(r"^[0-9a-f]{40}$")
_REPO_ID = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SOURCE_BINDING_FILENAME = "SOURCE_BINDING.json"


class PublicationSourceError(ValueError):
    """A source/staging rejection shared with the read-only drift verifier."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _git_bytes(root: Path, *arguments: str) -> bytes:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *arguments], check=True,
            capture_output=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise PublicationSourceError(
            "SOURCE_CHECKOUT_UNAVAILABLE", "Cannot read the immutable Git source."
        ) from exc


def _safe_source_path(value: str) -> bool:
    parts = value.split("/")
    return bool(value) and not any(
        part in {"", ".", "..", ".git", "__pycache__"} for part in parts
    ) and not any(char in value for char in "\\:\x00\r\n") and not any(
        ord(char) < 32 or ord(char) == 127 for char in value
    ) and not value.endswith((".pyc", ".pyo"))


def _source_snapshot(space_dir: Path, revision: str) -> dict[str, bytes]:
    """Read only exact tracked blobs; ignored local caches are never artifacts."""
    if not isinstance(revision, str) or not _SHA.fullmatch(revision):
        raise PublicationSourceError("SOURCE_CHECKOUT_MISMATCH", "An exact source SHA is required.")
    if space_dir.is_symlink() or not space_dir.is_dir():
        raise PublicationSourceError("SOURCE_TREE_UNSAFE", "The Space root must be a real directory.")
    space_dir = space_dir.resolve()
    root = space_dir.parent
    checkout_root = Path(_git_bytes(root, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if space_dir.name != "space" or checkout_root != root:
        raise PublicationSourceError("SOURCE_TREE_UNSAFE", "Only the canonical space subtree is admitted.")
    if _git_bytes(root, "rev-parse", "HEAD").decode().strip() != revision:
        raise PublicationSourceError("SOURCE_CHECKOUT_MISMATCH", "Checkout differs from the declared revision.")
    if _git_bytes(root, "status", "--porcelain=v1", "--untracked-files=all", "--", "space"):
        raise PublicationSourceError("SOURCE_WORKTREE_DIRTY", "The source subtree differs from its revision.")
    for path in space_dir.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise PublicationSourceError("SOURCE_TREE_UNSAFE", "Source links and special files are not admitted.")
    if (space_dir / SOURCE_BINDING_FILENAME).exists():
        raise PublicationSourceError("SOURCE_TREE_UNSAFE", "Source cannot override the generated binding.")

    snapshot: dict[str, bytes] = {}
    records = _git_bytes(root, "ls-tree", "-rz", "--full-tree", revision, "--", "space")
    for record in records.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, kind, oid = metadata.decode("ascii").split(" ")
            path = raw_path.decode("utf-8")
        except (ValueError, UnicodeError) as exc:
            raise PublicationSourceError("SOURCE_TREE_UNSAFE", "Malformed Git tree record.") from exc
        relative = path.removeprefix("space/")
        if (mode not in {"100644", "100755"} or kind != "blob"
                or not _SHA.fullmatch(oid) or not path.startswith("space/")
                or not _safe_source_path(relative) or relative == SOURCE_BINDING_FILENAME
                or relative in snapshot):
            raise PublicationSourceError("SOURCE_TREE_UNSAFE", "Unsafe or duplicate tracked source entry.")
        # Use the immutable blob, not a filesystem copy that can include caches
        # or change between discovery and staging.
        body = _git_bytes(root, "cat-file", "blob", oid)
        local = space_dir / relative
        if local.is_symlink() or not local.is_file() or local.read_bytes() != body:
            raise PublicationSourceError("SOURCE_WORKTREE_DIRTY", "Source bytes differ from the immutable blob.")
        snapshot[relative] = body
    if not snapshot:
        raise PublicationSourceError("SOURCE_TREE_EMPTY", "The tracked Space subtree is empty.")
    return dict(sorted(snapshot.items()))


def _validate_release_tree(release_dir: Path, expected: dict[str, bytes]) -> None:
    if release_dir.is_symlink() or not release_dir.is_dir():
        raise PublicationSourceError("PUBLICATION_OUTPUT_UNSAFE", "Release root must be a real directory.")
    observed: dict[str, bytes] = {}
    for path in release_dir.rglob("*"):
        relative = path.relative_to(release_dir).as_posix()
        if path.is_symlink() or not _safe_source_path(relative):
            raise PublicationSourceError("PUBLICATION_OUTPUT_UNSAFE", "Release contains an unsafe path.")
        if path.is_dir():
            if not any(name.startswith(relative + "/") for name in expected):
                raise PublicationSourceError("PUBLICATION_OUTPUT_SET_MISMATCH", "Release contains an unexpected directory.")
        elif path.is_file():
            observed[relative] = path.read_bytes()
        else:
            raise PublicationSourceError("PUBLICATION_OUTPUT_UNSAFE", "Release contains a special file.")
    if observed.keys() != expected.keys():
        raise PublicationSourceError("PUBLICATION_OUTPUT_SET_MISMATCH", "Release file set differs from the admitted output.")
    if observed != expected:
        raise PublicationSourceError("PUBLICATION_BYTE_DRIFT", "Release bytes differ from the immutable source.")


def _stage_release(release_dir: Path, snapshot: dict[str, bytes], binding: dict[str, object]) -> dict[str, bytes]:
    if any(not _safe_source_path(path) or path == SOURCE_BINDING_FILENAME for path in snapshot):
        raise PublicationSourceError("SOURCE_TREE_UNSAFE", "Unsafe source output path or binding collision.")
    expected = dict(snapshot)
    expected[SOURCE_BINDING_FILENAME] = (json.dumps(binding, indent=2, sort_keys=True) + "\n").encode("utf-8")
    release_dir.mkdir()  # The caller supplies a new, private temporary directory.
    for relative, body in sorted(expected.items()):
        target = release_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    _validate_release_tree(release_dir, expected)
    return expected


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
    snapshot = _source_snapshot(space_dir, str(binding["source_revision"]))
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
        expected = _stage_release(release_dir, snapshot, binding)
        _validate_release_tree(release_dir, expected)
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
    try:
        main()
    except PublicationSourceError as exc:
        sys.exit(f"{exc.code}: {exc}")
