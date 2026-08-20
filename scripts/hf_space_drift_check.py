#!/usr/bin/env python3
"""Fail-closed Hugging Face publication, deployment, and runtime verifier.

Acceptance requires four independent observations:

* the exact local source checkout and Space subtree;
* byte parity at one immutable Hugging Face Space revision;
* provider-reported deployment stage RUNNING at that revision; and
* a no-redirect readiness witness whose source attestation binds the same
  source SHA and Hugging Face revision.

The verifier is read-only. It never restarts, wakes, pauses, publishes, or
otherwise mutates a Space. Every terminal result is written as JSON evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

HUB_ORIGIN = "https://huggingface.co"
SOURCE_REPOSITORY = "szl-holdings/szl-router"
SOURCE_PATH = "space"
SOURCE_BINDING_FILENAME = "SOURCE_BINDING.json"
EVIDENCE_SCHEMA = "szl.hf-publication-runtime-parity/v1"
DEFAULT_EVIDENCE_FILE = "hf-space-parity-evidence.json"
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_TREE_ENTRIES = 1000
MAX_WITNESS_AGE_SECONDS = 120

_SHA = re.compile(r"^[0-9a-f]{40}$")
_REPO_ID = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_STAGE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_TRANSIENT_STAGES = {
    "BUILDING",
    "RUNNING_BUILDING",
    "STARTING",
    "APP_STARTING",
    "RUNNING_APP_STARTING",
}


class VerificationFailure(RuntimeError):
    """A sanitized, machine-classifiable verification failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = details or {}


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _exact_sha(value: object, *, field: str, code: str) -> str:
    candidate = str(value or "").strip().lower()
    if not _SHA.fullmatch(candidate):
        raise VerificationFailure(code, f"{field} is not an exact 40-character Git SHA.")
    return candidate


def _repo_path(repo_id: str) -> str:
    owner, name = repo_id.split("/", 1)
    return f"{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}"


def _provider_info_url(repo_id: str) -> str:
    return (
        f"{HUB_ORIGIN}/api/spaces/{_repo_path(repo_id)}"
        "?expand[]=sha&expand[]=subdomain"
    )


def _provider_runtime_url(repo_id: str) -> str:
    return f"{HUB_ORIGIN}/api/spaces/{_repo_path(repo_id)}/runtime"


def _tree_url(repo_id: str, hf_revision: str) -> str:
    return (
        f"{HUB_ORIGIN}/api/spaces/{_repo_path(repo_id)}/tree/{hf_revision}"
        f"?recursive=true&expand=false&limit={MAX_TREE_ENTRIES}"
    )


def _resolve_url(repo_id: str, hf_revision: str, path: str) -> str:
    quoted_path = urllib.parse.quote(path, safe="/")
    return (
        f"{HUB_ORIGIN}/spaces/{_repo_path(repo_id)}/resolve/"
        f"{hf_revision}/{quoted_path}"
    )


def _normalize_endpoint(value: str) -> tuple[str, str]:
    candidate = value.strip()
    try:
        parsed = urllib.parse.urlsplit(candidate)
    except ValueError as exc:
        raise VerificationFailure(
            "MISSING_OR_INVALID_CONFIG", "Space endpoint is malformed."
        ) from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise VerificationFailure(
            "MISSING_OR_INVALID_CONFIG",
            "Space endpoint must be an HTTPS origin with no credentials, path, query, or fragment.",
        )
    hostname = parsed.hostname.lower()
    if not hostname.endswith(".hf.space"):
        raise VerificationFailure(
            "MISSING_OR_INVALID_CONFIG",
            "Space endpoint must use the provider-owned hf.space origin.",
        )
    return f"https://{hostname}", hostname


def _number(
    value: object,
    *,
    field: str,
    minimum: float,
    maximum: float,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise VerificationFailure(
            "MISSING_OR_INVALID_CONFIG", f"{field} must be numeric."
        ) from exc
    if not minimum <= parsed <= maximum:
        raise VerificationFailure(
            "MISSING_OR_INVALID_CONFIG",
            f"{field} must be between {minimum:g} and {maximum:g} seconds.",
        )
    return parsed


def _run_git(source_root: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(source_root), *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise VerificationFailure(
            "SOURCE_CHECKOUT_UNAVAILABLE",
            "Unable to measure the local Git source checkout.",
        ) from exc
    return completed.stdout.strip()


def validate_config(args: argparse.Namespace) -> dict[str, object]:
    """Validate all required credentials/config before any network request."""

    repo_id = str(args.repo_id or "").strip()
    if not _REPO_ID.fullmatch(repo_id):
        raise VerificationFailure(
            "MISSING_OR_INVALID_CONFIG",
            "Hugging Face Space repo ID must be an explicit owner/name value.",
        )
    endpoint, endpoint_hostname = _normalize_endpoint(str(args.endpoint or ""))
    source_revision = _exact_sha(
        args.source_revision,
        field="Expected source revision",
        code="MISSING_OR_INVALID_CONFIG",
    )
    token = str(args.token or "").strip()
    if not token:
        raise VerificationFailure(
            "MISSING_CREDENTIALS",
            "A Hugging Face token is required for provider observations.",
        )

    witness_timeout = _number(
        args.witness_timeout_seconds,
        field="Witness timeout",
        minimum=1,
        maximum=900,
    )
    poll_interval = _number(
        args.poll_interval_seconds,
        field="Poll interval",
        minimum=0.1,
        maximum=30,
    )
    request_timeout = _number(
        args.request_timeout_seconds,
        field="Request timeout",
        minimum=0.5,
        maximum=60,
    )

    source_root = Path(args.source_root).resolve()
    requested_space_dir = Path(args.space_dir)
    if requested_space_dir.is_symlink():
        raise VerificationFailure(
            "SOURCE_TREE_UNSAFE", "Space source root cannot be a symbolic link."
        )
    space_dir = requested_space_dir.resolve()
    if not source_root.is_dir() or not space_dir.is_dir():
        raise VerificationFailure(
            "MISSING_OR_INVALID_CONFIG",
            "Source root and Space directory must both exist.",
        )
    try:
        relative_space_dir = space_dir.relative_to(source_root).as_posix()
    except ValueError as exc:
        raise VerificationFailure(
            "MISSING_OR_INVALID_CONFIG",
            "Space directory must be inside the source root.",
        ) from exc
    if relative_space_dir != SOURCE_PATH:
        raise VerificationFailure(
            "MISSING_OR_INVALID_CONFIG",
            f"Space directory must resolve to the {SOURCE_PATH!r} source subtree.",
        )

    checkout_revision = _exact_sha(
        _run_git(source_root, "rev-parse", "HEAD"),
        field="Local checkout revision",
        code="SOURCE_CHECKOUT_UNAVAILABLE",
    )
    if checkout_revision != source_revision:
        raise VerificationFailure(
            "SOURCE_CHECKOUT_MISMATCH",
            "Local checkout does not match the expected source revision.",
            details={
                "expected_source_revision": source_revision,
                "checkout_revision": checkout_revision,
            },
        )
    dirty = _run_git(
        source_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        SOURCE_PATH,
    )
    if dirty:
        raise VerificationFailure(
            "SOURCE_WORKTREE_DIRTY",
            "The Space source subtree differs from the exact checkout.",
        )

    return {
        "repo_id": repo_id,
        "endpoint": endpoint,
        "endpoint_hostname": endpoint_hostname,
        "source_repository": SOURCE_REPOSITORY,
        "source_revision": source_revision,
        "checkout_revision": checkout_revision,
        "source_root": source_root,
        "space_dir": space_dir,
        "token": token,
        "witness_timeout_seconds": witness_timeout,
        "poll_interval_seconds": poll_interval,
        "request_timeout_seconds": request_timeout,
    }


def new_evidence(config: dict[str, object] | None = None) -> dict[str, object]:
    config = config or {}
    return {
        "schema": EVIDENCE_SCHEMA,
        "started_at": _now_iso(),
        "observed_at": None,
        "verdict": "REJECTED",
        "authority_state": "READ_ONLY",
        "source": {
            "repository": config.get("source_repository", SOURCE_REPOSITORY),
            "revision": config.get("source_revision"),
            "checkout_revision": config.get("checkout_revision"),
            "path": SOURCE_PATH,
            "state": "UNVERIFIED",
        },
        "publication": {
            "provider": "HUGGING_FACE",
            "space": config.get("repo_id"),
            "revision": None,
            "revision_state": "UNVERIFIED",
            "source_binding_state": "UNVERIFIED",
            "bytes_state": "UNVERIFIED",
            "output_set_state": "UNVERIFIED",
            "expected_files": 0,
            "observed_files": 0,
            "missing_files": [],
            "unexpected_files": [],
            "files_checked": 0,
            "files": [],
        },
        "deployment": {
            "endpoint": config.get("endpoint"),
            "endpoint_binding_state": "UNVERIFIED",
            "provider_stage": None,
            "runtime_revision": None,
            "stage_state": "UNVERIFIED",
        },
        "runtime": {
            "readiness_url": (
                f"{config['endpoint']}/readyz" if config.get("endpoint") else None
            ),
            "readiness_http_status": None,
            "readiness_state": "UNVERIFIED",
            "redirect_policy": "REJECT",
            "source_attestation_url": (
                f"{config['endpoint']}/.well-known/szl-source.json?refresh=1"
                if config.get("endpoint")
                else None
            ),
            "attested_source_revision": None,
            "attested_hf_revision": None,
            "attestation_observed_at": None,
            "cache_policy_state": "UNVERIFIED",
            "witness_nonce": None,
            "witness_state": "UNVERIFIED",
        },
        "bindings": {
            "source_to_publication": "UNVERIFIED",
            "publication_to_runtime": "UNVERIFIED",
        },
        "truth_separation": {
            "source_state": "UNVERIFIED",
            "publication_state": "UNVERIFIED",
            "deployment_state": "UNVERIFIED",
            "runtime_state": "UNVERIFIED",
            "acceptance_state": "REJECTED",
        },
        "attempts": [],
        "failure": None,
    }


class HttpClient:
    """Bounded HTTP client with strict redirect handling for evidence routes."""

    def __init__(self, token: str) -> None:
        self._token = token
        self._strict_opener = urllib.request.build_opener(_RejectRedirects())
        self._content_opener = urllib.request.build_opener()

    def _request(
        self,
        url: str,
        *,
        timeout: float,
        authenticate: bool,
        allow_content_redirects: bool,
    ) -> tuple[bytes, int, str, str, dict[str, str]]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "szl-hf-runtime-parity/1.0",
        }
        if authenticate:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(url, headers=headers)
        opener = self._content_opener if allow_content_redirects else self._strict_opener
        try:
            with opener.open(request, timeout=timeout) as response:
                status = int(getattr(response, "status", response.getcode()))
                final_url = response.geturl()
                content_type = str(response.headers.get("Content-Type", ""))
                response_headers = {
                    str(key).lower(): str(value) for key, value in response.headers.items()
                }
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            is_redirect = 300 <= status < 400
            raise VerificationFailure(
                "HTTP_REDIRECT" if is_redirect else "HTTP_STATUS",
                (
                    "HTTP redirect rejected by witness policy."
                    if is_redirect
                    else f"HTTP request returned status {status}, expected 200."
                ),
                retryable=status in {408, 425, 429, 500, 502, 503, 504},
                details={
                    "status": status,
                    "location": exc.headers.get("Location") if is_redirect else None,
                },
            ) from exc
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            raise VerificationFailure(
                "HTTP_UNAVAILABLE",
                f"HTTP observation failed with {type(exc).__name__}.",
                retryable=True,
            ) from exc

        if status != 200:
            raise VerificationFailure(
                "HTTP_STATUS",
                f"HTTP request returned status {status}, expected 200.",
                retryable=status in {408, 425, 429, 500, 502, 503, 504},
                details={"status": status},
            )
        if not allow_content_redirects and final_url != url:
            raise VerificationFailure(
                "HTTP_REDIRECT",
                "HTTP redirect rejected by witness policy.",
                details={"final_url": final_url},
            )
        if len(body) > MAX_RESPONSE_BYTES:
            raise VerificationFailure(
                "HTTP_RESPONSE_TOO_LARGE",
                "HTTP response exceeded the bounded evidence size.",
            )
        return body, status, content_type, final_url, response_headers

    @staticmethod
    def _decode_json(body: bytes, content_type: str) -> object:
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type != "application/json" and not media_type.endswith("+json"):
            raise VerificationFailure(
                "MALFORMED_HTTP_RESPONSE",
                "Expected a JSON response; auth and error pages are not accepted.",
                details={"content_type": media_type or None},
            )
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise VerificationFailure(
                "MALFORMED_HTTP_RESPONSE", "HTTP response was not valid UTF-8 JSON."
            ) from exc

    def get_json(
        self,
        url: str,
        *,
        timeout: float,
        authenticate: bool,
    ) -> tuple[dict[str, object], int]:
        body, status, content_type, _, _ = self._request(
            url,
            timeout=timeout,
            authenticate=authenticate,
            allow_content_redirects=False,
        )
        payload = self._decode_json(body, content_type)
        if not isinstance(payload, dict):
            raise VerificationFailure(
                "MALFORMED_HTTP_RESPONSE", "JSON response root must be an object."
            )
        return payload, status

    def get_json_array(
        self,
        url: str,
        *,
        timeout: float,
        authenticate: bool,
    ) -> tuple[list[object], int]:
        body, status, content_type, _, _ = self._request(
            url,
            timeout=timeout,
            authenticate=authenticate,
            allow_content_redirects=False,
        )
        payload = self._decode_json(body, content_type)
        if not isinstance(payload, list):
            raise VerificationFailure(
                "MALFORMED_HTTP_RESPONSE", "JSON response root must be an array."
            )
        return payload, status

    def get_witness_json(
        self,
        url: str,
        *,
        timeout: float,
    ) -> tuple[dict[str, object], int, dict[str, str]]:
        body, status, content_type, _, headers = self._request(
            url,
            timeout=timeout,
            authenticate=False,
            allow_content_redirects=False,
        )
        payload = self._decode_json(body, content_type)
        if not isinstance(payload, dict):
            raise VerificationFailure(
                "MALFORMED_HTTP_RESPONSE", "JSON response root must be an object."
            )
        return payload, status, headers

    def get_bytes(
        self,
        url: str,
        *,
        timeout: float,
    ) -> tuple[bytes, int]:
        body, status, _, _, _ = self._request(
            url,
            timeout=timeout,
            authenticate=False,
            allow_content_redirects=True,
        )
        return body, status


def _provider_info(
    config: dict[str, object],
    client: HttpClient,
    *,
    timeout: float,
) -> tuple[str, str]:
    payload, _ = client.get_json(
        _provider_info_url(str(config["repo_id"])),
        timeout=timeout,
        authenticate=True,
    )
    revision = _exact_sha(
        payload.get("sha"),
        field="Provider-reported Space revision",
        code="MALFORMED_PROVIDER_RESPONSE",
    )
    subdomain = payload.get("subdomain")
    if not isinstance(subdomain, str) or not subdomain.strip():
        raise VerificationFailure(
            "MALFORMED_PROVIDER_RESPONSE",
            "Provider Space response omitted a valid subdomain.",
        )
    reported_hostname = subdomain.strip().lower()
    if "." not in reported_hostname:
        reported_hostname = f"{reported_hostname}.hf.space"
    if reported_hostname != config["endpoint_hostname"]:
        raise VerificationFailure(
            "ENDPOINT_CONFIG_MISMATCH",
            "Configured witness endpoint does not match the provider-reported Space subdomain.",
            details={
                "configured_hostname": config["endpoint_hostname"],
                "provider_hostname": reported_hostname,
            },
        )
    return revision, reported_hostname


def _provider_stage(
    config: dict[str, object],
    client: HttpClient,
    evidence: dict[str, object],
    *,
    expected_revision: str,
    timeout: float,
) -> str:
    payload, _ = client.get_json(
        _provider_runtime_url(str(config["repo_id"])),
        timeout=timeout,
        authenticate=True,
    )
    stage = payload.get("stage")
    if not isinstance(stage, str) or not stage or not _STAGE.fullmatch(stage):
        raise VerificationFailure(
            "MALFORMED_PROVIDER_RESPONSE",
            "Provider runtime response omitted a valid stage.",
        )
    runtime_revision = _exact_sha(
        payload.get("sha"),
        field="Provider runtime revision",
        code="MALFORMED_PROVIDER_RESPONSE",
    )
    deployment = evidence["deployment"]
    truth = evidence["truth_separation"]
    assert isinstance(deployment, dict)
    assert isinstance(truth, dict)
    deployment["provider_stage"] = stage
    deployment["runtime_revision"] = runtime_revision
    if stage != "RUNNING":
        deployment["stage_state"] = "REJECTED"
        truth["deployment_state"] = f"STAGE_{stage}"
        raise VerificationFailure(
            "PROVIDER_STAGE_NOT_RUNNING",
            f"Provider runtime stage is {stage}; exact RUNNING is required.",
            retryable=stage in _TRANSIENT_STAGES,
            details={"stage": stage},
        )
    if runtime_revision != expected_revision:
        deployment["stage_state"] = "REVISION_MISMATCH"
        truth["deployment_state"] = "RUNNING_STALE_REVISION"
        raise VerificationFailure(
            "PROVIDER_RUNTIME_REVISION_MISMATCH",
            "Provider runtime is RUNNING at a different Space revision.",
            retryable=True,
            details={
                "expected_revision": expected_revision,
                "runtime_revision": runtime_revision,
            },
        )
    deployment["stage_state"] = "ACCEPTED"
    truth["deployment_state"] = "PROVIDER_RUNNING"
    return stage


def _json_document(body: bytes, *, name: str) -> dict[str, object]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise VerificationFailure(
            "MALFORMED_PUBLICATION", f"Published {name} is not valid UTF-8 JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise VerificationFailure(
            "MALFORMED_PUBLICATION", f"Published {name} root must be an object."
        )
    return payload


def _verify_source_publication(
    config: dict[str, object],
    client: HttpClient,
    evidence: dict[str, object],
    hf_revision: str,
    timeout_for_call: Callable[[], float],
) -> None:
    space_dir = Path(config["space_dir"])
    source_paths = sorted(space_dir.rglob("*"))
    if any(path.is_symlink() for path in source_paths):
        raise VerificationFailure(
            "SOURCE_TREE_UNSAFE", "Symbolic links are not accepted in the Space source tree."
        )
    files = [path for path in source_paths if path.is_file()]
    if not files:
        raise VerificationFailure(
            "SOURCE_TREE_EMPTY", f"No files exist under the {SOURCE_PATH} subtree."
        )

    publication = evidence["publication"]
    source = evidence["source"]
    truth = evidence["truth_separation"]
    assert isinstance(publication, dict)
    assert isinstance(source, dict)
    assert isinstance(truth, dict)
    publication["files"] = []
    publication["files_checked"] = 0

    expected_paths = {path.relative_to(space_dir).as_posix() for path in files}
    expected_paths.add(SOURCE_BINDING_FILENAME)
    tree, _ = client.get_json_array(
        _tree_url(str(config["repo_id"]), hf_revision),
        timeout=timeout_for_call(),
        authenticate=True,
    )
    if len(tree) >= MAX_TREE_ENTRIES:
        raise VerificationFailure(
            "PUBLICATION_TREE_LIMIT",
            "Published tree reached the verifier entry limit; exact output set is unavailable.",
        )
    observed_paths: list[str] = []
    for entry in tree:
        if not isinstance(entry, dict) or entry.get("type") not in {"file", "directory"}:
            raise VerificationFailure(
                "MALFORMED_PROVIDER_RESPONSE",
                "Published tree contained an invalid entry.",
            )
        if entry["type"] != "file":
            continue
        path = entry.get("path")
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            raise VerificationFailure(
                "MALFORMED_PROVIDER_RESPONSE",
                "Published tree contained an unsafe file path.",
            )
        observed_paths.append(path)
    if len(observed_paths) != len(set(observed_paths)):
        raise VerificationFailure(
            "MALFORMED_PROVIDER_RESPONSE",
            "Published tree contained duplicate file paths.",
        )
    observed_set = set(observed_paths)
    missing = sorted(expected_paths - observed_set)
    unexpected = sorted(observed_set - expected_paths)
    publication["expected_files"] = len(expected_paths)
    publication["observed_files"] = len(observed_set)
    publication["missing_files"] = missing
    publication["unexpected_files"] = unexpected
    if missing or unexpected:
        publication["output_set_state"] = "DRIFT"
        truth["publication_state"] = "OUTPUT_SET_DRIFT"
        raise VerificationFailure(
            "PUBLICATION_OUTPUT_SET_MISMATCH",
            "Published immutable revision does not match the exact source output set.",
            details={"missing_files": missing, "unexpected_files": unexpected},
        )
    publication["output_set_state"] = "EXACT"

    for path in files:
        relative = path.relative_to(space_dir).as_posix()
        local_sha256 = _sha256_bytes(path.read_bytes())
        remote_body, _ = client.get_bytes(
            _resolve_url(str(config["repo_id"]), hf_revision, relative),
            timeout=timeout_for_call(),
        )
        remote_sha256 = _sha256_bytes(remote_body)
        file_evidence = {
            "path": relative,
            "source_sha256": local_sha256,
            "publication_sha256": remote_sha256,
            "state": "ALIGNED" if local_sha256 == remote_sha256 else "DRIFT",
        }
        cast_files = publication["files"]
        assert isinstance(cast_files, list)
        cast_files.append(file_evidence)
        publication["files_checked"] = int(publication["files_checked"]) + 1
        if local_sha256 != remote_sha256:
            publication["bytes_state"] = "DRIFT"
            truth["publication_state"] = "BYTE_DRIFT"
            raise VerificationFailure(
                "PUBLICATION_BYTE_DRIFT",
                f"Published bytes differ for {relative}.",
                details={"path": relative},
            )

    binding_body, _ = client.get_bytes(
        _resolve_url(
            str(config["repo_id"]),
            hf_revision,
            SOURCE_BINDING_FILENAME,
        ),
        timeout=timeout_for_call(),
    )
    binding = _json_document(binding_body, name=SOURCE_BINDING_FILENAME)
    expected_binding = {
        "schema": "szl.source-binding/v1",
        "source_repository": config["source_repository"],
        "source_revision": config["source_revision"],
        "source_path": SOURCE_PATH,
        "relation": "exact-deployed-subtree",
    }
    for key, expected in expected_binding.items():
        if binding.get(key) != expected:
            publication["source_binding_state"] = "MISMATCH"
            truth["publication_state"] = "SOURCE_BINDING_MISMATCH"
            raise VerificationFailure(
                "SOURCE_BINDING_MISMATCH",
                f"Published source binding mismatch for {key}.",
                details={"field": key, "expected": expected, "observed": binding.get(key)},
            )

    source["state"] = "EXACT_CHECKOUT"
    publication["bytes_state"] = "ALIGNED"
    publication["source_binding_state"] = "ALIGNED"
    truth["source_state"] = "EXACT_CHECKOUT"
    truth["publication_state"] = "EXACT_REVISION_ALIGNED"


def _require_fields(
    payload: dict[str, object],
    expected: dict[str, object],
    *,
    code: str,
    context: str,
    retryable: bool = False,
) -> None:
    for key, value in expected.items():
        if payload.get(key) != value:
            raise VerificationFailure(
                code,
                f"{context} mismatch for {key}.",
                retryable=retryable,
                details={"field": key, "expected": value, "observed": payload.get(key)},
            )


def _require_fresh_witness(
    headers: dict[str, str],
    payload: dict[str, object],
    *,
    context: str,
    require_timestamp: bool,
) -> None:
    directives = {
        directive.strip().lower()
        for directive in headers.get("cache-control", "").split(",")
        if directive.strip()
    }
    if "no-store" not in directives:
        raise VerificationFailure(
            "WITNESS_CACHE_POLICY_REJECTED",
            f"{context} did not require no-store caching.",
            retryable=True,
        )
    age = headers.get("age")
    if age is not None:
        try:
            cached_seconds = int(age)
        except ValueError as exc:
            raise VerificationFailure(
                "WITNESS_CACHE_POLICY_REJECTED",
                f"{context} returned an invalid Age header.",
                retryable=True,
            ) from exc
        if cached_seconds != 0:
            raise VerificationFailure(
                "WITNESS_CACHE_POLICY_REJECTED",
                f"{context} was served from a cache.",
                retryable=True,
            )
    if not require_timestamp:
        return
    observed_at = payload.get("observed_at")
    if not isinstance(observed_at, str):
        raise VerificationFailure(
            "WITNESS_FRESHNESS_REJECTED",
            f"{context} omitted observed_at.",
            retryable=True,
        )
    try:
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise VerificationFailure(
            "WITNESS_FRESHNESS_REJECTED",
            f"{context} returned an invalid observed_at.",
            retryable=True,
        ) from exc
    if observed.tzinfo is None:
        raise VerificationFailure(
            "WITNESS_FRESHNESS_REJECTED",
            f"{context} observed_at lacked a timezone.",
            retryable=True,
        )
    age_seconds = (datetime.now(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds()
    if age_seconds < -30 or age_seconds > MAX_WITNESS_AGE_SECONDS:
        raise VerificationFailure(
            "WITNESS_FRESHNESS_REJECTED",
            f"{context} was not observed within the freshness bound.",
            retryable=True,
            details={"age_seconds": round(age_seconds, 3)},
        )


def _verify_runtime_witness(
    config: dict[str, object],
    client: HttpClient,
    evidence: dict[str, object],
    hf_revision: str,
    timeout_for_call: Callable[[], float],
) -> None:
    endpoint = str(config["endpoint"])
    nonce = str(config.get("witness_nonce") or secrets.token_hex(16))
    readiness_url = f"{endpoint}/readyz?{urllib.parse.urlencode({'nonce': nonce})}"
    readiness, readiness_status, readiness_headers = client.get_witness_json(
        readiness_url,
        timeout=timeout_for_call(),
    )
    _require_fresh_witness(
        readiness_headers,
        readiness,
        context="Readiness witness",
        require_timestamp=False,
    )
    _require_fields(
        readiness,
        {
            "transport_state": "REACHABLE",
            "evidence_state": "OBSERVED",
            "verification_state": "SOURCE_BOUND",
            "authority_state": "READ_ONLY",
            "status": "ready",
            "service": "llm-router-live",
            "source_binding": "SOURCE_BOUND",
        },
        code="READINESS_CONTRACT_REJECTED",
        context="Readiness witness",
        retryable=True,
    )

    attestation_url = (
        f"{endpoint}/.well-known/szl-source.json?"
        + urllib.parse.urlencode({"refresh": "1", "nonce": nonce})
    )
    attestation, _, attestation_headers = client.get_witness_json(
        attestation_url,
        timeout=timeout_for_call(),
    )
    _require_fresh_witness(
        attestation_headers,
        attestation,
        context="Runtime source attestation",
        require_timestamp=True,
    )
    _require_fields(
        attestation,
        {
            "schema": "szl.deployment-source/v1",
            "transport_state": "REACHABLE",
            "verification_state": "SOURCE_BOUND",
            "authority_state": "READ_ONLY",
            "alignment_state": "SOURCE_BOUND_DEPLOYMENT",
        },
        code="RUNTIME_ATTESTATION_REJECTED",
        context="Runtime source attestation",
        retryable=True,
    )
    source = attestation.get("source")
    deployment = attestation.get("deployment")
    if not isinstance(source, dict) or not isinstance(deployment, dict):
        raise VerificationFailure(
            "RUNTIME_ATTESTATION_REJECTED",
            "Runtime source attestation omitted source or deployment objects.",
        )
    _require_fields(
        source,
        {
            "repository": config["source_repository"],
            "commit": config["source_revision"],
            "path": SOURCE_PATH,
            "relation": "exact-deployed-subtree",
            "state": "SOURCE_BOUND",
        },
        code="STALE_SOURCE_REVISION",
        context="Runtime source binding",
        retryable=True,
    )
    _require_fields(
        deployment,
        {
            "hf_space": config["repo_id"],
            "hf_revision": hf_revision,
            "revision_state": "MEASURED",
        },
        code="STALE_RUNTIME_REVISION",
        context="Runtime Hugging Face revision",
        retryable=True,
    )
    if deployment.get("measurement_method") not in {
        "SPACE_REPOSITORY_COMMIT",
        "HUGGINGFACE_API",
    }:
        raise VerificationFailure(
            "RUNTIME_ATTESTATION_REJECTED",
            "Runtime Hugging Face revision measurement method is unavailable or unknown.",
        )

    runtime = evidence["runtime"]
    truth = evidence["truth_separation"]
    assert isinstance(runtime, dict)
    assert isinstance(truth, dict)
    runtime["readiness_url"] = readiness_url
    runtime["source_attestation_url"] = attestation_url
    runtime["readiness_http_status"] = readiness_status
    runtime["readiness_state"] = "READY"
    runtime["attested_source_revision"] = source["commit"]
    runtime["attested_hf_revision"] = deployment["hf_revision"]
    runtime["attestation_observed_at"] = attestation["observed_at"]
    runtime["cache_policy_state"] = "NO_STORE_FRESH"
    runtime["witness_nonce"] = nonce
    runtime["witness_state"] = "SOURCE_AND_HF_REVISION_BOUND"
    truth["runtime_state"] = "READY_WITNESSED"


def verify_once(
    config: dict[str, object],
    client: HttpClient,
    evidence: dict[str, object],
    timeout_for_call: Callable[[], float],
) -> None:
    """Run one complete, race-bracketed verification attempt."""

    publication = evidence["publication"]
    deployment = evidence["deployment"]
    bindings = evidence["bindings"]
    truth = evidence["truth_separation"]
    assert isinstance(publication, dict)
    assert isinstance(deployment, dict)
    assert isinstance(bindings, dict)
    assert isinstance(truth, dict)

    hf_revision, _ = _provider_info(
        config, client, timeout=timeout_for_call()
    )
    publication["revision"] = hf_revision
    publication["revision_state"] = "OBSERVED"
    deployment["endpoint_binding_state"] = "PROVIDER_MATCHED"
    _provider_stage(
        config,
        client,
        evidence,
        expected_revision=hf_revision,
        timeout=timeout_for_call(),
    )

    _verify_source_publication(
        config,
        client,
        evidence,
        hf_revision,
        timeout_for_call,
    )
    _verify_runtime_witness(
        config,
        client,
        evidence,
        hf_revision,
        timeout_for_call,
    )

    final_revision, _ = _provider_info(
        config, client, timeout=timeout_for_call()
    )
    if final_revision != hf_revision:
        publication["revision_state"] = "CHANGED_DURING_WITNESS"
        truth["publication_state"] = "STALE_OR_RACING_REVISION"
        raise VerificationFailure(
            "STALE_OR_RACING_REVISION",
            "Provider Space revision changed during the runtime witness.",
            retryable=True,
            details={
                "initial_hf_revision": hf_revision,
                "final_hf_revision": final_revision,
            },
        )
    _provider_stage(
        config,
        client,
        evidence,
        expected_revision=hf_revision,
        timeout=timeout_for_call(),
    )

    publication["revision_state"] = "EXACT_STABLE"
    bindings["source_to_publication"] = "EXACT"
    bindings["publication_to_runtime"] = "EXACT"
    truth["acceptance_state"] = "ACCEPTED"
    evidence["verdict"] = "ACCEPTED"


def verify_with_polling(
    config: dict[str, object],
    client: HttpClient,
    evidence: dict[str, object],
) -> None:
    deadline = time.monotonic() + float(config["witness_timeout_seconds"])

    def timeout_for_call() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise VerificationFailure(
                "WITNESS_TIMEOUT",
                "Bounded provider/runtime witness timed out before acceptance.",
            )
        return min(float(config["request_timeout_seconds"]), remaining)

    attempt_number = 0
    while True:
        attempt_number += 1
        try:
            verify_once(config, client, evidence, timeout_for_call)
        except VerificationFailure as exc:
            attempts = evidence["attempts"]
            deployment = evidence["deployment"]
            publication = evidence["publication"]
            assert isinstance(attempts, list)
            assert isinstance(deployment, dict)
            assert isinstance(publication, dict)
            attempts.append(
                {
                    "attempt": attempt_number,
                    "observed_at": _now_iso(),
                    "state": "REJECTED",
                    "code": exc.code,
                    "retryable": exc.retryable,
                    "provider_stage": deployment.get("provider_stage"),
                    "hf_revision": publication.get("revision"),
                }
            )
            remaining = deadline - time.monotonic()
            if not exc.retryable:
                raise
            if remaining <= 0:
                raise VerificationFailure(
                    "WITNESS_TIMEOUT",
                    "Bounded provider/runtime witness timed out without acceptance.",
                    details={"last_failure": exc.code},
                ) from exc
            time.sleep(min(float(config["poll_interval_seconds"]), remaining))
            continue

        attempts = evidence["attempts"]
        assert isinstance(attempts, list)
        attempts.append(
            {
                "attempt": attempt_number,
                "observed_at": _now_iso(),
                "state": "ACCEPTED",
                "code": None,
                "retryable": False,
                "provider_stage": "RUNNING",
                "hf_revision": evidence["publication"]["revision"],
            }
        )
        return


def _record_failure(evidence: dict[str, object], failure: VerificationFailure) -> None:
    evidence["verdict"] = "REJECTED"
    truth = evidence["truth_separation"]
    assert isinstance(truth, dict)
    truth["acceptance_state"] = "REJECTED"
    evidence["failure"] = {
        "code": failure.code,
        "message": str(failure),
        "retryable": failure.retryable,
        "details": failure.details,
    }


def _write_evidence(path: Path, evidence: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--space-dir", default="space")
    parser.add_argument("--source-root", default=".")
    parser.add_argument("--repo-id", default="")
    parser.add_argument("--endpoint", default="")
    parser.add_argument("--source-revision", default="")
    parser.add_argument("--token", default="")
    parser.add_argument("--upstream-conclusion", default="success")
    parser.add_argument("--evidence-file", default=DEFAULT_EVIDENCE_FILE)
    parser.add_argument("--witness-timeout-seconds", default="300")
    parser.add_argument("--poll-interval-seconds", default="5")
    parser.add_argument("--request-timeout-seconds", default="15")
    return parser


def main() -> int:
    args = _parser().parse_args()
    evidence_path = Path(args.evidence_file or DEFAULT_EVIDENCE_FILE).resolve()
    evidence = new_evidence(
        {
            "repo_id": str(args.repo_id or "").strip() or None,
            "endpoint": str(args.endpoint or "").strip() or None,
            "source_repository": SOURCE_REPOSITORY,
            "source_revision": str(args.source_revision or "").strip().lower() or None,
        }
    )

    try:
        upstream_conclusion = str(args.upstream_conclusion or "").strip().lower()
        if upstream_conclusion != "success":
            raise VerificationFailure(
                "UPSTREAM_DEPLOYMENT_FAILED",
                "The upstream Hugging Face deployment workflow did not succeed.",
                details={"conclusion": upstream_conclusion or None},
            )
        config = validate_config(args)
        evidence = new_evidence(config)
        verify_with_polling(config, HttpClient(str(config["token"])), evidence)
    except VerificationFailure as exc:
        _record_failure(evidence, exc)
    except Exception as exc:  # noqa: BLE001
        _record_failure(
            evidence,
            VerificationFailure(
                "INTERNAL_VERIFIER_ERROR",
                f"Verifier failed closed with {type(exc).__name__}.",
            ),
        )
    finally:
        evidence["observed_at"] = _now_iso()
        _write_evidence(evidence_path, evidence)
        print(json.dumps(evidence, sort_keys=True))

    if evidence["verdict"] != "ACCEPTED":
        failure = evidence.get("failure")
        code = failure.get("code") if isinstance(failure, dict) else "UNKNOWN"
        print(
            f"Hugging Face publication/runtime parity rejected ({code}); "
            f"evidence: {evidence_path}",
            file=sys.stderr,
        )
        return 1
    print(f"Hugging Face publication/runtime parity accepted; evidence: {evidence_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
