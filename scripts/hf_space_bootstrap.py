#!/usr/bin/env python3
"""Create the one canonical Router Space only when it is genuinely absent.

This is a bounded bootstrap, not a general-purpose Hugging Face provisioner. It
accepts exactly ``SZLHOLDINGS/llm-router-live``, runs only for the exact protected
``main`` source revision, creates no alternate repository, reads no provider
credentials into evidence, and performs no deletion or hardware change.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any

TARGET_REPO_ID = "SZLHOLDINGS/llm-router-live"
_SHA = re.compile(r"^[0-9a-f]{40}$")


def _require_protected_main(ref: str, workflow_sha: str, source_revision: str) -> str:
    revision = source_revision.strip().lower()
    if ref != "refs/heads/main":
        sys.exit("Router Space bootstrap is restricted to refs/heads/main.")
    if not _SHA.fullmatch(revision):
        sys.exit("Router Space bootstrap requires an exact 40-character source SHA.")
    if workflow_sha.strip().lower() != revision:
        sys.exit("Router Space bootstrap source revision does not match GITHUB_SHA.")
    return revision


def _exact_revision(value: Any) -> str:
    revision = str(value or "").strip().lower()
    if not _SHA.fullmatch(revision):
        sys.exit("Canonical Router Space has no exact immutable revision after bootstrap.")
    return revision


def ensure_canonical_space(
    api: Any,
    repo_id: str,
    *,
    not_found_error: type[BaseException],
) -> dict[str, object]:
    """Resolve or create exactly one fixed Space, then read it back."""

    if repo_id.strip() != TARGET_REPO_ID:
        sys.exit(f"Bootstrap may create only {TARGET_REPO_ID}.")

    created = False
    try:
        info = api.repo_info(repo_id=TARGET_REPO_ID, repo_type="space")
    except not_found_error:
        api.create_repo(
            repo_id=TARGET_REPO_ID,
            repo_type="space",
            private=False,
            exist_ok=True,
            space_sdk="docker",
        )
        created = True
        info = api.repo_info(repo_id=TARGET_REPO_ID, repo_type="space")

    return {
        "repo_id": TARGET_REPO_ID,
        "space_created": created,
        "observed_revision": _exact_revision(getattr(info, "sha", "")),
        "private": bool(getattr(info, "private", True)),
    }


def main() -> None:
    from huggingface_hub import HfApi
    from huggingface_hub.errors import RepositoryNotFoundError

    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-id", default=TARGET_REPO_ID)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args()

    source_revision = _require_protected_main(
        os.environ.get("GITHUB_REF", ""),
        os.environ.get("GITHUB_SHA", ""),
        args.source_revision,
    )
    if not args.token.strip():
        sys.exit("HF_TOKEN is required for canonical Router Space bootstrap.")

    result = ensure_canonical_space(
        HfApi(token=args.token),
        args.repo_id,
        not_found_error=RepositoryNotFoundError,
    )
    result.update(
        {
            "schema": "szl.router-space-bootstrap/v1",
            "source_revision": source_revision,
            "exact_target_only": True,
            "hardware_changed": False,
            "visibility_changed": False,
            "credential_value_recorded": False,
        }
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
