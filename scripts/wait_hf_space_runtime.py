#!/usr/bin/env python3
"""Wait for a Hugging Face Space runtime to converge to its published revision.

Fresh Docker Space publications can briefly return a valid repository revision
while the runtime endpoint still reports ``stage: null`` and ``sha: null``. That
is a provider transition state, not proof of a malformed permanent response.

This helper performs one bounded, read-only wait before the stricter publication
and live-witness verifier runs. It accepts only the canonical Router Space,
requires public visibility, an exact repository SHA, an exact runtime SHA, stage
``RUNNING``, and equality between the two revisions. Missing metadata and known
build/start states are retryable. Non-empty malformed values and terminal error
states fail closed.

The Hugging Face token is read only from the command-line argument supplied by a
masked GitHub Actions secret. Its value is never logged or written to evidence.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

HUB_ORIGIN = "https://huggingface.co"
CANONICAL_REPO_ID = "SZLHOLDINGS/llm-router-live"
SCHEMA = "szl.hf-runtime-convergence/v1"
MAX_RESPONSE_BYTES = 1024 * 1024
_SHA = re.compile(r"^[0-9a-f]{40}$")
_STAGE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_TRANSIENT_STAGES = {
    "APP_STARTING",
    "BUILDING",
    "PENDING",
    "QUEUED",
    "RESTARTING",
    "RUNNING_APP_STARTING",
    "RUNNING_BUILDING",
    "STARTING",
}


class RuntimeWaitError(RuntimeError):
    """Sanitized failure with retry semantics."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = details or {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _repo_path(repo_id: str) -> str:
    owner, name = repo_id.split("/", 1)
    return f"{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}"


def provider_info_url(repo_id: str) -> str:
    return (
        f"{HUB_ORIGIN}/api/spaces/{_repo_path(repo_id)}"
        "?expand[]=sha&expand[]=private"
    )


def provider_runtime_url(repo_id: str) -> str:
    return f"{HUB_ORIGIN}/api/spaces/{_repo_path(repo_id)}/runtime"


def _exact_sha_or_pending(value: object, *, field: str) -> str:
    if value is None or value == "":
        raise RuntimeWaitError(
            "RUNTIME_METADATA_PENDING",
            f"{field} is not populated yet.",
            retryable=True,
            details={"field": field},
        )
    candidate = str(value).strip().lower()
    if not _SHA.fullmatch(candidate):
        raise RuntimeWaitError(
            "MALFORMED_PROVIDER_RESPONSE",
            f"{field} is non-empty but is not an exact 40-character Git SHA.",
            retryable=False,
            details={"field": field},
        )
    return candidate


def classify_observation(
    info: dict[str, object],
    runtime: dict[str, object],
) -> dict[str, object]:
    """Classify one provider observation without performing network I/O."""

    private = info.get("private")
    if private is None:
        raise RuntimeWaitError(
            "VISIBILITY_METADATA_PENDING",
            "Provider visibility metadata is not populated yet.",
            retryable=True,
        )
    if not isinstance(private, bool):
        raise RuntimeWaitError(
            "MALFORMED_PROVIDER_RESPONSE",
            "Provider visibility metadata is not boolean.",
            retryable=False,
        )
    if private:
        raise RuntimeWaitError(
            "SPACE_VISIBILITY_PENDING",
            "Provider still reports the Router Space as private.",
            retryable=True,
        )

    publication_revision = _exact_sha_or_pending(
        info.get("sha"),
        field="Provider repository revision",
    )

    stage_value = runtime.get("stage")
    if stage_value is None or stage_value == "":
        raise RuntimeWaitError(
            "RUNTIME_METADATA_PENDING",
            "Provider runtime stage is not populated yet.",
            retryable=True,
            details={"publication_revision": publication_revision},
        )
    if not isinstance(stage_value, str):
        raise RuntimeWaitError(
            "MALFORMED_PROVIDER_RESPONSE",
            "Provider runtime stage is non-empty but not a string.",
            retryable=False,
        )
    stage = stage_value.strip().upper()
    if not _STAGE.fullmatch(stage):
        raise RuntimeWaitError(
            "MALFORMED_PROVIDER_RESPONSE",
            "Provider runtime stage is non-empty but malformed.",
            retryable=False,
            details={"stage": stage},
        )

    runtime_revision = _exact_sha_or_pending(
        runtime.get("sha"),
        field="Provider runtime revision",
    )

    if stage in _TRANSIENT_STAGES:
        raise RuntimeWaitError(
            "PROVIDER_RUNTIME_TRANSITION",
            f"Provider runtime stage is {stage}.",
            retryable=True,
            details={
                "stage": stage,
                "publication_revision": publication_revision,
                "runtime_revision": runtime_revision,
            },
        )
    if stage != "RUNNING":
        raise RuntimeWaitError(
            "PROVIDER_RUNTIME_TERMINAL_STATE",
            f"Provider runtime stage is {stage}; exact RUNNING is required.",
            retryable=False,
            details={
                "stage": stage,
                "publication_revision": publication_revision,
                "runtime_revision": runtime_revision,
            },
        )
    if runtime_revision != publication_revision:
        raise RuntimeWaitError(
            "RUNTIME_REVISION_PENDING",
            "Provider runtime has not converged to the current repository revision.",
            retryable=True,
            details={
                "stage": stage,
                "publication_revision": publication_revision,
                "runtime_revision": runtime_revision,
            },
        )

    return {
        "stage": stage,
        "publication_revision": publication_revision,
        "runtime_revision": runtime_revision,
        "private": False,
    }


class ProviderClient:
    """Bounded authenticated JSON reader for Hugging Face provider metadata."""

    def __init__(self, token: str) -> None:
        token = token.strip()
        if not token:
            raise RuntimeWaitError(
                "MISSING_CREDENTIALS",
                "A Hugging Face token is required.",
                retryable=False,
            )
        self._token = token

    def get_json(self, url: str, *, timeout: float) -> dict[str, object]:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._token}",
                "Cache-Control": "no-cache",
                "User-Agent": "szl-hf-runtime-convergence/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = int(getattr(response, "status", response.getcode()))
                content_type = str(response.headers.get("Content-Type", ""))
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            raise RuntimeWaitError(
                "PROVIDER_HTTP_STATUS",
                f"Provider metadata request returned HTTP {status}.",
                retryable=status in {404, 408, 425, 429, 500, 502, 503, 504},
                details={"status": status},
            ) from exc
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            raise RuntimeWaitError(
                "PROVIDER_UNAVAILABLE",
                f"Provider metadata request failed with {type(exc).__name__}.",
                retryable=True,
            ) from exc

        if status != 200:
            raise RuntimeWaitError(
                "PROVIDER_HTTP_STATUS",
                f"Provider metadata request returned HTTP {status}.",
                retryable=status in {404, 408, 425, 429, 500, 502, 503, 504},
                details={"status": status},
            )
        if len(body) > MAX_RESPONSE_BYTES:
            raise RuntimeWaitError(
                "PROVIDER_RESPONSE_TOO_LARGE",
                "Provider metadata response exceeded the one-megabyte limit.",
                retryable=False,
            )
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type != "application/json" and not media_type.endswith("+json"):
            raise RuntimeWaitError(
                "MALFORMED_PROVIDER_RESPONSE",
                "Provider metadata response was not JSON.",
                retryable=False,
                details={"content_type": media_type or None},
            )
        try:
            payload: Any = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeWaitError(
                "MALFORMED_PROVIDER_RESPONSE",
                "Provider metadata response was not valid UTF-8 JSON.",
                retryable=False,
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeWaitError(
                "MALFORMED_PROVIDER_RESPONSE",
                "Provider metadata response root was not an object.",
                retryable=False,
            )
        return payload


def wait_for_runtime(
    *,
    repo_id: str,
    client: ProviderClient,
    timeout_seconds: float,
    poll_interval_seconds: float,
    request_timeout_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    if repo_id != CANONICAL_REPO_ID:
        raise RuntimeWaitError(
            "TARGET_REJECTED",
            f"This helper only accepts {CANONICAL_REPO_ID}.",
            retryable=False,
        )
    if not 1 <= timeout_seconds <= 900:
        raise RuntimeWaitError(
            "INVALID_CONFIG",
            "Timeout must be between 1 and 900 seconds.",
            retryable=False,
        )
    if not 0.1 <= poll_interval_seconds <= 30:
        raise RuntimeWaitError(
            "INVALID_CONFIG",
            "Poll interval must be between 0.1 and 30 seconds.",
            retryable=False,
        )
    if not 0.5 <= request_timeout_seconds <= 60:
        raise RuntimeWaitError(
            "INVALID_CONFIG",
            "Request timeout must be between 0.5 and 60 seconds.",
            retryable=False,
        )

    started_at = _now()
    deadline = monotonic() + timeout_seconds
    attempts: list[dict[str, object]] = []
    last_failure: RuntimeWaitError | None = None

    while True:
        attempt_number = len(attempts) + 1
        try:
            remaining = max(deadline - monotonic(), 0.0)
            if remaining <= 0:
                break
            call_timeout = min(request_timeout_seconds, max(remaining, 0.5))
            info = client.get_json(provider_info_url(repo_id), timeout=call_timeout)
            runtime = client.get_json(provider_runtime_url(repo_id), timeout=call_timeout)
            observation = classify_observation(info, runtime)
            attempts.append(
                {
                    "attempt": attempt_number,
                    "observed_at": _now(),
                    "state": "CONVERGED",
                    "stage": observation["stage"],
                    "publication_revision": observation["publication_revision"],
                    "runtime_revision": observation["runtime_revision"],
                }
            )
            return {
                "schema": SCHEMA,
                "repo_id": repo_id,
                "started_at": started_at,
                "observed_at": _now(),
                "status": "CONVERGED",
                "observation": observation,
                "attempts": attempts,
                "credential_value_recorded": False,
            }
        except RuntimeWaitError as exc:
            last_failure = exc
            attempts.append(
                {
                    "attempt": attempt_number,
                    "observed_at": _now(),
                    "state": "WAITING" if exc.retryable else "REJECTED",
                    "code": exc.code,
                    "retryable": exc.retryable,
                    "details": exc.details,
                }
            )
            if not exc.retryable:
                raise RuntimeWaitError(
                    exc.code,
                    str(exc),
                    retryable=False,
                    details={"attempts": attempts, **exc.details},
                ) from exc

        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        sleep(min(poll_interval_seconds, remaining))

    raise RuntimeWaitError(
        "RUNTIME_CONVERGENCE_TIMEOUT",
        "Provider runtime did not reach public RUNNING revision parity before timeout.",
        retryable=False,
        details={
            "attempts": attempts,
            "last_code": last_failure.code if last_failure else None,
        },
    )


def _write_receipt(path: Path | None, payload: dict[str, object]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-id", default=CANONICAL_REPO_ID)
    parser.add_argument("--token", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=900)
    parser.add_argument("--poll-interval-seconds", type=float, default=10)
    parser.add_argument("--request-timeout-seconds", type=float, default=15)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()

    try:
        receipt = wait_for_runtime(
            repo_id=args.repo_id,
            client=ProviderClient(args.token),
            timeout_seconds=args.timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
            request_timeout_seconds=args.request_timeout_seconds,
        )
    except RuntimeWaitError as exc:
        receipt = {
            "schema": SCHEMA,
            "repo_id": args.repo_id,
            "observed_at": _now(),
            "status": "REJECTED",
            "failure": {
                "code": exc.code,
                "message": str(exc),
                "details": exc.details,
            },
            "credential_value_recorded": False,
        }
        _write_receipt(args.receipt, receipt)
        print(json.dumps(receipt, indent=2, sort_keys=True), file=sys.stderr)
        return 1

    _write_receipt(args.receipt, receipt)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
