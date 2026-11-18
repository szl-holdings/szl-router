#!/usr/bin/env python3
"""Offline only: produce a private Salad/Massed coexistence review, not a route.

Input is a redacted, explicit-identity snapshot. Nothing reads provider
credentials, contacts a node, creates jobs, enables inference or changes money.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from router_control.coexistence_admission import Hold, assess, input_json


def write_exclusive(path: Path, result: dict) -> str:
    if path.is_symlink() or not path.parent.is_dir():
        raise Hold("OUTPUT_DIRECTORY_UNAVAILABLE")
    if path.resolve().is_relative_to(ROOT.resolve()):
        raise Hold("SOURCE_TREE_OUTPUT_FORBIDDEN")
    blob = (
        json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    with path.open("xb") as stream:
        stream.write(blob)
        stream.flush()
        os.fsync(stream.fileno())
    return hashlib.sha256(blob).hexdigest()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.snapshot.is_symlink():
            raise Hold("SYMLINKED_INPUT_NOT_ADMITTED")
        with args.snapshot.open("rb") as stream:
            raw = stream.read(64_001)
        snapshot = input_json(raw)
        result = assess(snapshot, now=datetime.now(timezone.utc))
        digest = write_exclusive(args.output, result)
    except (Hold, OSError) as exc:
        reason = str(exc) if isinstance(exc, Hold) else type(exc).__name__
        print(json.dumps({"state": "HOLD", "reason": reason}))
        return 2
    print(
        json.dumps(
            {
                "state": result["state"],
                "advisory_candidate_host": result["advisory_candidate_host"],
                "output_sha256": digest,
                "execution_authorized": False,
                "new_salad_replicas_authorized": 0,
                "provider_mutations": 0,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
