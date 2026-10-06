#!/usr/bin/env python3
"""Publish and witness the separate, egress-disabled router control Space.

Only protected szl-router main may write SZLHOLDINGS/szl-router-control. The
Space contains a generated Dockerfile pinned to a verified public GHCR digest;
it never contains provider configuration, credentials, or copied application
source. An image signature and provenance check precede this script in CI.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

TARGET = "SZLHOLDINGS/szl-router-control"
SOURCE_REPOSITORY = "szl-holdings/szl-router"
IMAGE = "ghcr.io/szl-holdings/szl-router-control"
ENDPOINT = "https://szlholdings-szl-router-control.hf.space"
SCHEMA = "szl.router-control-space/v1"
FILES = frozenset({"README.md", "Dockerfile", "SOURCE_BINDING.json"})
PROVIDER_METADATA = ".gitattributes"
BOOTSTRAP_BINDING = Path(__file__).resolve().parents[1] / "publishing/router-control-bootstrap.v1.json"
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
TRANSIENT = {"BUILDING", "APP_STARTING", "STARTING", "QUEUED", "PENDING", "RESTARTING", "RUNNING_BUILDING", "RUNNING_APP_STARTING"}
TERMINAL = {"BUILD_ERROR", "RUNTIME_ERROR", "CONFIG_ERROR", "ERROR", "STOPPED"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


class ControlSpaceError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def exact_sha(value: object, code: str) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ControlSpaceError(code)
    return value


def exact_digest(value: object) -> str:
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise ControlSpaceError("IMAGE_DIGEST_INVALID")
    return value


def git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], check=True, capture_output=True, text=True, timeout=20
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise ControlSpaceError("SOURCE_GIT_UNAVAILABLE") from exc


def require_protected_main(revision: str) -> None:
    exact_sha(revision, "SOURCE_REVISION_INVALID")
    if (os.environ.get("GITHUB_REPOSITORY") != SOURCE_REPOSITORY
            or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_SHA") != revision):
        raise ControlSpaceError("SOURCE_CONTEXT_REJECTED")
    if git("rev-parse", "HEAD") != revision or git("status", "--porcelain=v1"):
        raise ControlSpaceError("SOURCE_CHECKOUT_MISMATCH")
    remote = git("ls-remote", "https://github.com/szl-holdings/szl-router.git", "refs/heads/main")
    if remote.split("\t", 1)[0] != revision:
        raise ControlSpaceError("SOURCE_MAIN_SUPERSEDED")


def _json_bytes(value: dict[str, object]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def space_files(revision: str, digest: str) -> dict[str, bytes]:
    exact_sha(revision, "SOURCE_REVISION_INVALID")
    exact_digest(digest)
    binding: dict[str, object] = {
        "schema": SCHEMA,
        "target": TARGET,
        "source_repository": SOURCE_REPOSITORY,
        "source_revision": revision,
        "image_reference": f"{IMAGE}@{digest}",
        "image_digest": digest,
        "egress_policy": "DENIED",
        "inference_state": "UNAVAILABLE_UNCONFIGURED",
    }
    readme = f"""---
title: SZL Router Control
sdk: docker
app_port: 7860
license: apache-2.0
short_description: Source-bound control interface with inference disabled.
---

# SZL Router Control

This is the public control-interface canary for
[`szl-holdings/szl-router`](https://github.com/szl-holdings/szl-router).
It uses the published image `{IMAGE}@{digest}` from source `{revision}`.
This publisher supplies no provider credentials and forces inference egress off.
It verifies that no provider registry is configured at runtime. `/readyz`
measures only control-interface readiness; `/readyz/inference` must return 503.
The separate `SZLHOLDINGS/llm-router-live` status Space is independently owned.
"""
    dockerfile = f"""FROM {IMAGE}@{digest}
ENV SZL_ROUTER_ENABLE_EGRESS=0
CMD ["sh", "-c", "exec env SZL_ROUTER_ENABLE_EGRESS=0 uvicorn router_control.app:app --host 0.0.0.0 --port 7860 --workers 1 --no-access-log"]
"""
    return {
        "README.md": readme.encode("utf-8"),
        "Dockerfile": dockerfile.encode("utf-8"),
        "SOURCE_BINDING.json": _json_bytes(binding),
    }


def _request_json(url: str, *, token: str | None = None, status: int = 200,
                  no_store: bool = False) -> dict[str, Any]:
    headers = {"Accept": "application/json", "Cache-Control": "no-store", "User-Agent": "szl-router-control-space/1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        response = _NO_REDIRECT_OPENER.open(request, timeout=15)
    except urllib.error.HTTPError as exc:
        response = exc
    except (OSError, urllib.error.URLError) as exc:
        raise ControlSpaceError("HTTP_WITNESS_UNAVAILABLE") from exc
    try:
        with response:
            if response.status in {401, 403}:
                raise ControlSpaceError("HTTP_AUTH_REJECTED")
            if response.status != status or response.geturl().split("?", 1)[0] != url.split("?", 1)[0]:
                raise ControlSpaceError("HTTP_WITNESS_REJECTED")
            if no_store and "no-store" not in response.headers.get("Cache-Control", "").lower():
                raise ControlSpaceError("HTTP_CACHE_CONTRACT_REJECTED")
            data = response.read(131073)
            if len(data) > 131072:
                raise ControlSpaceError("HTTP_WITNESS_TOO_LARGE")
            payload = json.loads(data)
    except (OSError, ValueError) as exc:
        raise ControlSpaceError("HTTP_WITNESS_UNAVAILABLE") from exc
    if not isinstance(payload, dict):
        raise ControlSpaceError("HTTP_WITNESS_MALFORMED")
    return payload


def anonymous_image_manifest(digest: str) -> None:
    """Require a public exact registry manifest, without downloading layers."""
    exact_digest(digest)
    token_url = ("https://ghcr.io/token?service=ghcr.io&scope="
                 "repository%3Aszl-holdings%2Fszl-router-control%3Apull")
    try:
        token = _request_json(token_url).get("token")
        if not isinstance(token, str) or not token:
            raise ControlSpaceError("IMAGE_NOT_PUBLIC")
        request = urllib.request.Request(
            f"https://ghcr.io/v2/szl-holdings/szl-router-control/manifests/{digest}",
            method="HEAD",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json",
            },
        )
        with _NO_REDIRECT_OPENER.open(request, timeout=15) as response:
            observed = response.headers.get("Docker-Content-Digest")
            if response.status != 200 or observed != digest:
                raise ControlSpaceError("IMAGE_PUBLIC_DIGEST_MISMATCH")
    except (OSError, urllib.error.URLError) as exc:
        raise ControlSpaceError("IMAGE_NOT_PUBLIC") from exc


def _repo_identity(info: Any) -> str:
    if getattr(info, "id", None) != TARGET:
        raise ControlSpaceError("SPACE_IDENTITY_MISMATCH")
    if getattr(info, "sdk", None) != "docker":
        raise ControlSpaceError("SPACE_SDK_MISMATCH")
    return exact_sha(getattr(info, "sha", None), "SPACE_REVISION_INVALID")


def _download_bytes(download: Callable[..., str], filename: str, revision: str, token: str) -> bytes:
    path = download(repo_id=TARGET, filename=filename, repo_type="space", revision=revision, token=token)
    data = Path(path).read_bytes()
    if len(data) > 131072:
        raise ControlSpaceError("SPACE_FILE_TOO_LARGE")
    return data


def _verify_files(api: Any, download: Callable[..., str], revision: str,
                  token: str, expected: dict[str, bytes], provider_metadata: bytes) -> None:
    observed = set(api.list_repo_files(repo_id=TARGET, repo_type="space", revision=revision))
    if observed != FILES | {PROVIDER_METADATA}:
        raise ControlSpaceError("SPACE_FILE_SET_MISMATCH")
    for filename, body in expected.items():
        if _download_bytes(download, filename, revision, token) != body:
            raise ControlSpaceError("SPACE_FILE_BYTE_MISMATCH")
    if _download_bytes(download, PROVIDER_METADATA, revision, token) != provider_metadata:
        raise ControlSpaceError("SPACE_PROVIDER_METADATA_DRIFT")


def _existing_target_admitted(api: Any, download: Callable[..., str],
                              revision: str, token: str) -> bytes:
    observed = set(api.list_repo_files(repo_id=TARGET, repo_type="space", revision=revision))
    if observed == {PROVIDER_METADATA, "README.md"}:
        return _recover_bootstrap_metadata(download, revision, token, observed)
    if observed != FILES | {PROVIDER_METADATA}:
        raise ControlSpaceError("EXISTING_SPACE_FILE_SET_REJECTED")
    try:
        binding = json.loads(_download_bytes(download, "SOURCE_BINDING.json", revision, token))
    except (ValueError, OSError) as exc:
        raise ControlSpaceError("EXISTING_SPACE_BINDING_REJECTED") from exc
    if (not isinstance(binding, dict) or binding.get("schema") != SCHEMA
            or binding.get("target") != TARGET
            or binding.get("source_repository") != SOURCE_REPOSITORY
            or binding.get("egress_policy") != "DENIED"):
        raise ControlSpaceError("EXISTING_SPACE_BINDING_REJECTED")
    try:
        old_expected = space_files(binding.get("source_revision"), binding.get("image_digest"))
    except ControlSpaceError as exc:
        raise ControlSpaceError("EXISTING_SPACE_BINDING_REJECTED") from exc
    for filename, body in old_expected.items():
        if _download_bytes(download, filename, revision, token) != body:
            raise ControlSpaceError("EXISTING_SPACE_BYTES_REJECTED")
    return _download_bytes(download, PROVIDER_METADATA, revision, token)


def _recover_bootstrap_metadata(download: Callable[..., str], revision: str,
                                token: str, observed: set[str]) -> bytes:
    """Admit only the source-reviewed scaffold from the recorded failed creation."""
    try:
        binding = json.loads(BOOTSTRAP_BINDING.read_bytes())
        if (binding.get("schema") != "szl.router-control-bootstrap/v1"
                or binding.get("target") != TARGET
                or binding.get("source_repository") != SOURCE_REPOSITORY
                or binding.get("parent_revision") != revision
                or set(binding.get("files_sha256", {})) != observed):
            raise ValueError("bootstrap binding mismatch")
        files = {name: _download_bytes(download, name, revision, token) for name in observed}
        for name, body in files.items():
            if hashlib.sha256(body).hexdigest() != binding["files_sha256"][name]:
                raise ValueError("bootstrap bytes mismatch")
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise ControlSpaceError("EXISTING_SPACE_BOOTSTRAP_REJECTED") from exc
    return files[PROVIDER_METADATA]


def validate_card(readme: bytes) -> None:
    from huggingface_hub import SpaceCard

    try:
        SpaceCard(readme.decode("utf-8")).validate()
    except ValueError as exc:
        raise ControlSpaceError("SPACE_CARD_METADATA_INVALID") from exc


def _new_target_metadata(api: Any, download: Callable[..., str], revision: str,
                         token: str) -> bytes:
    """Freeze the Hub system commit's metadata before any upload."""
    observed = set(api.list_repo_files(repo_id=TARGET, repo_type="space", revision=revision))
    if observed not in ({PROVIDER_METADATA}, {PROVIDER_METADATA, "README.md"}):
        raise ControlSpaceError("NEW_SPACE_SYSTEM_FILES_REJECTED")
    return _download_bytes(download, PROVIDER_METADATA, revision, token)


def _runtime_aligned(token: str, revision: str) -> bool:
    runtime = _request_json(
        "https://huggingface.co/api/spaces/SZLHOLDINGS/szl-router-control/runtime",
        token=token,
    )
    stage = runtime.get("stage")
    observed = runtime.get("sha")
    if stage in TERMINAL:
        raise ControlSpaceError("SPACE_RUNTIME_TERMINAL")
    if stage is None or stage in TRANSIENT or stage == "PAUSED":
        return False
    if stage != "RUNNING":
        raise ControlSpaceError("SPACE_RUNTIME_UNKNOWN")
    if observed is None:
        return False
    return exact_sha(observed, "SPACE_RUNTIME_SHA_INVALID") == revision


def _witness(revision: str) -> None:
    nonce = urllib.parse.quote(os.urandom(12).hex())
    deployment = _request_json(f"{ENDPOINT}/deployment.json?nonce={nonce}", no_store=True)
    ready = _request_json(f"{ENDPOINT}/readyz?nonce={nonce}", no_store=True)
    inference = _request_json(f"{ENDPOINT}/readyz/inference?nonce={nonce}", status=503,
                              no_store=True)
    if (deployment.get("schema") != "szl.router-deployment/v1"
            or deployment.get("source_revision") != revision
            or deployment.get("egress_enabled") is not False
            or deployment.get("registry_state") != "UNAVAILABLE_NOT_CONFIGURED"
            or ready.get("status") != "ready"
            or ready.get("scope") != "CONTROL_PLANE"
            or ready.get("egress_enabled") is not False
            or inference.get("ready_for_requests") is not False
            or inference.get("inference_witness") != "UNAVAILABLE"):
        raise ControlSpaceError("SPACE_RUNTIME_CONTRACT_MISMATCH")


def publish(api: Any, download: Callable[..., str], *, token: str,
            source_revision: str, image_digest: str, timeout_seconds: int = 900,
            attempt: dict[str, object] | None = None,
            checkpoint: Callable[[dict[str, object]], None] | None = None) -> dict[str, object]:
    receipt = attempt if attempt is not None else {}

    def record(**changes: object) -> None:
        receipt.update(changes)
        if checkpoint is not None:
            checkpoint(receipt)

    record(schema=SCHEMA, status="BLOCKED", target=TARGET, endpoint=ENDPOINT,
           source_repository=SOURCE_REPOSITORY,
           source_revision=source_revision, image_digest=image_digest,
           image_reference=f"{IMAGE}@{image_digest}", phase="PREFLIGHT",
           write_outcome="NOT_ATTEMPTED", creation_outcome="NOT_ATTEMPTED",
           credential_value_recorded=False)
    if not token.strip():
        raise ControlSpaceError("HF_TOKEN_MISSING")
    expected = space_files(source_revision, image_digest)
    require_protected_main(source_revision)
    anonymous_image_manifest(image_digest)
    validate_card(expected["README.md"])
    record(card_validation="MEASURED")

    from huggingface_hub.errors import RepositoryNotFoundError
    created = False
    try:
        before = api.repo_info(repo_id=TARGET, repo_type="space")
    except RepositoryNotFoundError as exc:
        if getattr(getattr(exc, "response", None), "status_code", None) != 404:
            raise ControlSpaceError("SPACE_ACCESS_REJECTED") from exc
        record(phase="CREATE_REQUESTED", creation_outcome="UNKNOWN")
        api.create_repo(repo_id=TARGET, repo_type="space", private=False,
                        exist_ok=False, space_sdk="docker")
        created = True
        record(phase="CREATED", space_created=True, creation_outcome="MEASURED")
        before = api.repo_info(repo_id=TARGET, repo_type="space")
    parent = _repo_identity(before)
    record(parent_revision=parent, space_created=created)
    if getattr(before, "private", None) is not False:
        raise ControlSpaceError("SPACE_VISIBILITY_REJECTED")
    api.auth_check(repo_id=TARGET, repo_type="space", write=True)
    provider_metadata = (
        _new_target_metadata(api, download, parent, token) if created
        else _existing_target_admitted(api, download, parent, token)
    )
    # The write is bounded to one target and one immutable parent revision.
    require_protected_main(source_revision)
    with tempfile.TemporaryDirectory(prefix="szl-router-control-space-") as temporary:
        folder = Path(temporary)
        for filename, body in expected.items():
            (folder / filename).write_bytes(body)
        record(phase="UPLOAD_REQUESTED", write_outcome="UNKNOWN")
        commit = api.upload_folder(
            repo_id=TARGET, repo_type="space", folder_path=str(folder),
            commit_message=f"deploy: router control {source_revision} {image_digest}",
            parent_commit=parent, delete_patterns="*",
        )
    published = exact_sha(getattr(commit, "oid", None), "SPACE_PUBLISH_SHA_INVALID")
    record(phase="UPLOADED", published_revision=published, write_outcome="MEASURED",
           provider_metadata_sha256=hashlib.sha256(provider_metadata).hexdigest())
    after = api.repo_info(repo_id=TARGET, repo_type="space")
    if _repo_identity(after) != published or getattr(after, "private", None) is not False:
        raise ControlSpaceError("SPACE_PUBLISH_READBACK_MISMATCH")
    _verify_files(api, download, published, token, expected, provider_metadata)
    record(phase="FILES_VERIFIED", file_readback="MEASURED")

    deadline = time.monotonic() + timeout_seconds
    while True:
        after = api.repo_info(repo_id=TARGET, repo_type="space")
        if _repo_identity(after) != published or getattr(after, "private", None) is not False:
            raise ControlSpaceError("SPACE_PUBLISH_READBACK_MISMATCH")
        try:
            aligned = _runtime_aligned(token, published)
        except ControlSpaceError as exc:
            if exc.code not in {"HTTP_WITNESS_REJECTED", "HTTP_WITNESS_UNAVAILABLE"}:
                raise
            aligned = False
        if aligned:
            try:
                _witness(source_revision)
                break
            except ControlSpaceError as exc:
                if exc.code not in {"HTTP_WITNESS_REJECTED", "HTTP_WITNESS_UNAVAILABLE"}:
                    raise
        if time.monotonic() >= deadline:
            raise ControlSpaceError("SPACE_RUNTIME_TIMEOUT")
        time.sleep(min(10, max(0, deadline - time.monotonic())))
    require_protected_main(source_revision)
    record(status="MEASURED", phase="RUNTIME_VERIFIED", provider_stage="RUNNING",
           control_readiness="MEASURED", inference="UNAVAILABLE",
           inference_reason="UNCONFIGURED_EGRESS_DENIED")
    return receipt


def write_receipt(path: Path, receipt: dict[str, object]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(_json_bytes(receipt))
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    result: dict[str, object] = {
        "schema": SCHEMA, "status": "BLOCKED", "target": TARGET,
        "endpoint": ENDPOINT, "source_repository": SOURCE_REPOSITORY,
        "source_revision": args.source_revision, "image_digest": args.image_digest,
        "phase": "PREFLIGHT", "write_outcome": "NOT_ATTEMPTED",
        "creation_outcome": "NOT_ATTEMPTED", "credential_value_recorded": False,
    }
    write_receipt(args.receipt, result)
    try:
        from huggingface_hub import HfApi, hf_hub_download
        token = os.environ.get("HF_TOKEN", "")
        publish(HfApi(token=token), hf_hub_download, token=token,
                source_revision=args.source_revision, image_digest=args.image_digest,
                attempt=result, checkpoint=lambda value: write_receipt(args.receipt, value))
    except ControlSpaceError as exc:
        result.update(status="BLOCKED", failure_code=exc.code)
    except Exception as exc:  # Provider errors can include headers; never serialize them.
        result.update(status="UNKNOWN", failure_code=type(exc).__name__)
    write_receipt(args.receipt, result)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "MEASURED" else 1


if __name__ == "__main__":
    sys.exit(main())
