#!/usr/bin/env python3
"""Drift guard: assert the live llm-router-live Space matches szl-router/space.

Walks every file under `space/`, fetches the same path from the Space's
`resolve/main/<path>` URL, and asserts sha256 equality. Any mismatch, missing
live file, or extra-tracked file fails the job. Stdlib only (no token needed —
public Space). This is the digest-level proof that GitHub remains source-of-truth.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

RESOLVE = "https://huggingface.co/spaces/{repo}/resolve/main/{path}"


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--space-dir", default="space")
    ap.add_argument("--repo-id", default="SZLHOLDINGS/llm-router-live")
    args = ap.parse_args()

    space_dir = Path(args.space_dir).resolve()
    files = sorted(p for p in space_dir.rglob("*") if p.is_file())
    if not files:
        sys.exit(f"No files under {space_dir}.")

    ok = True
    for p in files:
        rel = p.relative_to(space_dir).as_posix()
        local = _sha256_bytes(p.read_bytes())
        url = RESOLVE.format(repo=args.repo_id, path=rel)
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                live = _sha256_bytes(r.read())
        except Exception as e:  # noqa: BLE001
            print(f"MISSING-LIVE  {rel}  ({e})")
            ok = False
            continue
        status = "OK" if local == live else "MISMATCH"
        if status != "OK":
            ok = False
        print(f"{local[:16]}  {live[:16]}  {status}  {rel}")

    if not ok:
        sys.exit("Drift detected: live Space != szl-router/space.")
    print(f"\nAll {len(files)} files aligned: live llm-router-live == szl-router/space.")


if __name__ == "__main__":
    main()
