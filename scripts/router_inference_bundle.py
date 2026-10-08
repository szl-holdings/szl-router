#!/usr/bin/env python3
"""Prepare a disabled inference staging bundle offline; never publish or activate."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from router_control.app import Registry, validate_base_url

REPOSITORY = "szl-holdings/szl-router"
IMAGE = "ghcr.io/szl-holdings/szl-router-control"
SCHEMA = "szl.router-inference-staging/v1"
SHA = re.compile(r"[0-9a-f]{40}")
IMAGE_REFERENCE = re.compile(re.escape(IMAGE) + r"@sha256:[0-9a-f]{64}")
ORIGINS = frozenset({
    "https://github.com/szl-holdings/szl-router",
    "https://github.com/szl-holdings/szl-router.git",
    "git@github.com:szl-holdings/szl-router",
    "git@github.com:szl-holdings/szl-router.git",
})
SECRET_NAMES = ("SZL_ROUTER_TOKEN", "SZL_ROUTER_OPENAI_TOKEN")
MANIFEST = "BUNDLE_MANIFEST.json"


class BundleError(RuntimeError):
    """Only fixed error codes are exposed; subprocess errors may contain secrets."""


def _json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def validate_inputs(source_revision: str, image_reference: str) -> None:
    if not isinstance(source_revision, str) or not SHA.fullmatch(source_revision):
        raise BundleError("SOURCE_REVISION_INVALID")
    if not isinstance(image_reference, str) or not IMAGE_REFERENCE.fullmatch(image_reference):
        raise BundleError("IMAGE_REFERENCE_INVALID")


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(ROOT), *args], check=True,
                              capture_output=True, text=True, timeout=15).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise BundleError("LOCAL_SOURCE_UNAVAILABLE") from None


def require_source(source_revision: str) -> None:
    if (_git("remote", "get-url", "origin") not in ORIGINS
            or Path(_git("rev-parse", "--show-toplevel")).resolve() != ROOT.resolve()
            or _git("rev-parse", "HEAD") != source_revision):
        raise BundleError("LOCAL_SOURCE_MISMATCH")
    if _git("status", "--porcelain=v1", "--untracked-files=all"):
        raise BundleError("LOCAL_SOURCE_DIRTY")


def disabled_registry() -> dict[str, Any]:
    candidate = {"providers": [{
        "id": "openai-dedicated-project", "provider_type": "openai_https",
        "base_url": "https://api.openai.com/v1", "models": {"szl-astra": "gpt-6-astra"},
        "token_env": "SZL_ROUTER_OPENAI_TOKEN", "priority": 100, "sovereignty": 0,
        "cost_tier": 10, "classifications": ["public"], "enabled": False,
    }]}
    registry = Registry.model_validate(candidate)
    provider = registry.providers[0]
    if (provider.enabled or provider.classifications != ["public"]
            or validate_base_url(provider.base_url, frozenset({"api.openai.com"})) != provider.base_url):
        raise BundleError("DISABLED_REGISTRY_INVALID")
    return candidate


def bundle_files(source_revision: str, image_reference: str) -> dict[str, bytes]:
    """Deterministic bytes; digest syntax is not image authenticity or authorization."""
    validate_inputs(source_revision, image_reference)
    registry = disabled_registry()
    environment = {
        "SZL_ROUTER_ENABLE_EGRESS": "0", "SZL_ROUTER_ALLOWED_HOSTS": "api.openai.com",
        "SZL_ROUTER_PROVIDERS_JSON": json.dumps(registry, sort_keys=True, separators=(",", ":")),
    }
    # Fixed Python argv avoids shell interpolation and resets inherited configuration.
    launch = (
        "import os,sys;"
        + "".join(f"os.environ.pop({name!r},None);" for name in SECRET_NAMES)
        + f"os.environ.update({environment!r});"
        + "os.execv(sys.executable,[sys.executable,'-I','-m','uvicorn','router_control.app:app',"
        "'--app-dir','/app','--host','0.0.0.0','--port','7860','--workers','1','--no-access-log'])"
    )
    binding = {
        "schema": SCHEMA, "evidence_class": "DECLARED", "mode": "PREPARE_ONLY",
        "source_repository": REPOSITORY, "source_revision": source_revision,
        "source_authenticity": "UNKNOWN", "remote_main_admission": "UNKNOWN",
        "image_reference": image_reference, "image_authenticity": "UNKNOWN",
        "image_source_alignment": "UNKNOWN", "target": None,
        "deployment_authorized": False, "inference_state": "UNAVAILABLE",
        "egress_enabled": False, "provider_enabled": False,
        "required_secret_names": list(SECRET_NAMES), "secret_values_included": False,
        "preserved_targets": ["SZLHOLDINGS/szl-router-control", "SZLHOLDINGS/llm-router-live"],
        "spend_admission": "BLOCKED", "cost_tier_is_usd_cap": False,
        "model_identity": "UNKNOWN", "runtime_witness": "UNAVAILABLE",
        "activation": "Requires a separately reviewed artifact; environment overrides cannot activate this command",
    }
    dockerfile = (
        f"FROM {image_reference}\n"
        "ENV SZL_ROUTER_ENABLE_EGRESS=0\n"
        "CMD " + json.dumps(["python", "-I", "-c", launch]) + "\n"
    )
    readme = """# Disabled Router Inference Staging Bundle

DECLARED preparation only. This directory is not a deployment or activation.
The referenced image's signature, provenance, source alignment and current-main
admission are UNKNOWN until independently verified by the release workflow.

The fixed default command replaces inherited registry and host configuration,
forces egress off, and unsets the two named dedicated credentials before launch.
The sole registry candidate is disabled, public-data-only OpenAI text routing.
No prompt, provider request, paid operation, remote write, or secret read occurs
during preparation. Cost tier 10 is an ordinal, not a dollar cap.

No Space target is selected. Do not upload this bundle to the existing control
or status Spaces. Their publishers and runtime policies remain unchanged.
Runtime readiness is not provider reachability, model identity or an answer.
Only an exact-source, separately governed deployment and authorized acceptance
can establish those facts. No model-quality or production claim is made here.

Only a directory with BUNDLE_MANIFEST.json and matching file hashes is complete.
Preparation reserves a new directory and never overwrites it. Failed preparation
may retain a partial directory without that completion manifest; preserve it.
Changing the image, command, entrypoint, mounted code, or container privileges is
outside this disabled-command contract. It is not a tamper-proof boundary.
"""
    return {"Dockerfile": dockerfile.encode(), "README.md": readme.encode(),
            "providers.disabled.json": _json(registry), "SOURCE_BINDING.json": _json(binding)}


def _plain_directory(path: Path) -> os.stat_result:
    for item in (*reversed(path.parents), path):
        try:
            info = item.lstat()
        except OSError:
            raise BundleError("OUTPUT_PARENT_UNAVAILABLE") from None
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise BundleError("OUTPUT_LINK_OR_NON_DIRECTORY")
    return info


def _new_output(output: Path) -> tuple[Path, tuple[int, int]]:
    if ".." in output.parts:
        raise BundleError("OUTPUT_TRAVERSAL_REJECTED")
    output = output.absolute()
    if output.is_relative_to(ROOT.resolve()):
        raise BundleError("OUTPUT_INSIDE_SOURCE_REJECTED")
    _plain_directory(output.parent)
    try:
        output.mkdir(exist_ok=False)
    except FileExistsError:
        raise BundleError("OUTPUT_ALREADY_EXISTS") from None
    except OSError:
        raise BundleError("OUTPUT_CREATE_FAILED") from None
    info = _plain_directory(output)
    return output, (info.st_dev, info.st_ino)


def _write_new(output: Path, identity: tuple[int, int], name: str, body: bytes) -> None:
    info = _plain_directory(output)
    if (info.st_dev, info.st_ino) != identity:
        raise BundleError("OUTPUT_DIRECTORY_CHANGED")
    try:
        with (output / name).open("xb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        raise BundleError("OUTPUT_WRITE_FAILED") from None


def prepare_bundle(source_revision: str, image_reference: str, output: Path) -> dict[str, Any]:
    validate_inputs(source_revision, image_reference)
    require_source(source_revision)
    files = bundle_files(source_revision, image_reference)
    output, identity = _new_output(output)
    for name, body in files.items():
        _write_new(output, identity, name, body)
    require_source(source_revision)
    manifest = {
        "schema": SCHEMA, "evidence_class": "MEASURED", "operation": "LOCAL_PREPARATION",
        "source_revision": source_revision, "local_checkout_match": True,
        "image_reference": image_reference, "image_authenticity": "UNKNOWN",
        "source_authenticity": "UNKNOWN", "remote_main_admission": "UNKNOWN",
        "deployment_authorized": False, "remote_writes": 0, "inference_calls": 0,
        "file_sha256": {name: hashlib.sha256(body).hexdigest() for name, body in sorted(files.items())},
    }
    # The completion marker is written last, exclusively; partial output is retained.
    _write_new(output, identity, MANIFEST, _json(manifest))
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--image-reference", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = prepare_bundle(args.source_revision, args.image_reference, args.output)
    except BundleError as exc:
        print(json.dumps({"evidence_class": "BLOCKED", "code": str(exc), "deployment_authorized": False}))
        return 1
    except Exception:
        print(json.dumps({"evidence_class": "BLOCKED", "code": "PREPARATION_FAILED", "deployment_authorized": False}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
