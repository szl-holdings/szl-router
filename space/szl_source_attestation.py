"""Honest deployment-source attestation shared by the small public Spaces.

The deployed Hugging Face revision is measured independently of any GitHub
source observation.  A source reference never implies artifact parity or a
reproducible build.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
from datetime import datetime, timezone


_SHA = re.compile(r"^[0-9a-f]{40}$")
_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, dict[str, object]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _valid_sha(value: object) -> str | None:
    candidate = str(value or "").strip().lower()
    return candidate if _SHA.fullmatch(candidate) else None


def measure_hf_revision(space_id: str, force: bool = False) -> dict[str, object]:
    env_revision = _valid_sha(os.environ.get("SPACE_REPOSITORY_COMMIT"))
    if env_revision:
        return {
            "hf_revision": env_revision,
            "revision_state": "MEASURED",
            "measurement_method": "SPACE_REPOSITORY_COMMIT",
        }

    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(space_id)
        if not force and cached and now - float(cached["stored_at"]) < 60:
            return dict(cached["measurement"])  # type: ignore[arg-type]

    revision = None
    error = None
    req = urllib.request.Request(
        f"https://huggingface.co/api/spaces/{space_id}?expand[]=sha",
        headers={"Accept": "application/json", "User-Agent": "szl-source-attestation/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as response:
            revision = _valid_sha(json.load(response).get("sha"))
    except Exception as exc:
        error = type(exc).__name__

    measurement: dict[str, object] = {
        "hf_revision": revision,
        "revision_state": "MEASURED" if revision else "UNAVAILABLE",
        "measurement_method": "HUGGINGFACE_API" if revision else "UNAVAILABLE",
    }
    if error:
        measurement["measurement_error"] = error
    with _CACHE_LOCK:
        _CACHE[space_id] = {"stored_at": time.monotonic(), "measurement": dict(measurement)}
    return measurement


def build_attestation(
    space_id: str,
    source: dict[str, object],
    alignment_state: str,
    force: bool = False,
) -> dict[str, object]:
    measurement = measure_hf_revision(space_id, force=force)
    return {
        "schema": "szl.deployment-source/v1",
        "observed_at": _now_iso(),
        "transport_state": "REACHABLE",
        "evidence_state": "COMPUTED" if measurement["hf_revision"] else "UNAVAILABLE",
        "verification_state": "STRUCTURAL_ONLY",
        "authority_state": "READ_ONLY",
        "source": dict(source),
        "deployment": {"hf_space": space_id, **measurement},
        "alignment_state": alignment_state,
        "attestation_state": "UNSIGNED_STRUCTURAL",
        "claims": {
            "github_parity": "NOT_CLAIMED",
            "reproducible_build": "NOT_CLAIMED",
        },
        "limits": [
            "The Hugging Face revision is measured independently from the source observation.",
            "A GitHub reference does not establish deployed-artifact equivalence.",
            "This unsigned structural attestation does not claim a reproducible build or GitHub parity.",
        ],
    }
