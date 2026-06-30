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
import sys
from pathlib import Path

from huggingface_hub import HfApi


def _validate_readme(space_dir: Path) -> None:
    readme = space_dir / "README.md"
    text = readme.read_text(encoding="utf-8")
    if not text.startswith("---"):
        sys.exit("README.md has no YAML front-matter — refusing to deploy.")
    fm = text.split("---", 2)[1]
    for required in ("sdk: docker", "app_port: 7860"):
        if required not in fm:
            sys.exit(f"README front-matter missing `{required}` — refusing to deploy.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--space-dir", default="space")
    ap.add_argument("--repo-id", default="SZLHOLDINGS/llm-router-live")
    ap.add_argument("--token", required=True)
    ap.add_argument("--commit-message", default="deploy: sync status Space from szl-router/space")
    args = ap.parse_args()

    space_dir = Path(args.space_dir).resolve()
    if not space_dir.is_dir():
        sys.exit(f"{space_dir} is not a directory.")
    _validate_readme(space_dir)

    api = HfApi(token=args.token)
    api.upload_folder(
        repo_id=args.repo_id,
        repo_type="space",
        folder_path=str(space_dir),
        commit_message=args.commit_message,
    )
    print(f"Deployed {space_dir} -> space/{args.repo_id}")


if __name__ == "__main__":
    main()
