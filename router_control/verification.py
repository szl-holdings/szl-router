# SPDX-License-Identifier: Apache-2.0
"""Unsigned completion integrity verification; never identity or model attestation.

Offline usage: python -m router_control.verification completion-bundle.json
The JSON bundle has exactly two objects: ``completion`` and original ``request``.
Exit codes: 0 consistent, 1 divergent, 2 invalid input. No provider is contacted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

MAX_VERIFICATION_BYTES = 3_000_000
MAX_COMPLETION_BYTES = 2_032_768
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 100_000
DIGEST = re.compile(r"^[0-9a-f]{64}$")


class VerificationInputError(ValueError):
    """Bounded, sanitized invalid input error."""


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False, default=str).encode("utf-8")


def sha256(value: Any) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def parse_bundle(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_VERIFICATION_BYTES:
        raise VerificationInputError("verification input exceeds byte limit")
    def reject_constant(_: str) -> None:
        raise VerificationInputError("non-finite JSON is forbidden")
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = dict(pairs)
        if len(result) != len(pairs):
            raise VerificationInputError("duplicate JSON keys are forbidden")
        return result
    try:
        bundle = json.loads(raw.decode("utf-8"), parse_constant=reject_constant,
                            object_pairs_hook=unique_object)
        stack = [(bundle, 0)]
        nodes = 0
        while stack:
            value, depth = stack.pop()
            nodes += 1
            if depth > MAX_JSON_DEPTH or nodes + len(stack) > MAX_JSON_NODES:
                raise VerificationInputError("verification input exceeds structure limit")
            if isinstance(value, dict):
                stack.extend((item, depth + 1) for item in value.values())
            elif isinstance(value, list):
                stack.extend((item, depth + 1) for item in value)
        if (not isinstance(bundle, dict) or set(bundle) != {"completion", "request"}
                or not all(isinstance(bundle[key], dict) for key in bundle)):
            raise VerificationInputError("expected completion and request objects")
        return bundle
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise VerificationInputError("verification input must be bounded UTF-8 JSON") from exc


def verify_completion(completion: dict[str, Any], normalized_request: dict[str, Any],
                      receipt_header: str | None = None) -> dict[str, Any]:
    """Check digest bindings using the original ChatRequest.model_dump(mode='json').

    Recomputing all unsigned hashes can manufacture a consistent bundle. No
    provider identity, model weights, plan policy or answer quality is verified.
    """
    try:
        if len(canonical(completion)) > MAX_COMPLETION_BYTES:
            raise VerificationInputError("completion exceeds byte limit")
        receipt = completion.get("szl_receipt")
        receipt = receipt if isinstance(receipt, dict) else {}
        body = {key: value for key, value in receipt.items() if key not in {"digest", "algorithm"}}
        upstream = {key: value for key, value in completion.items() if key != "szl_receipt"}
        checks = {
            "receipt_digest": (receipt.get("schema") == "szl.router-receipt/v1"
                               and receipt.get("algorithm") == "sha256"
                               and receipt.get("digest") == sha256(body)),
            "response_digest": receipt.get("response_digest") == sha256(upstream),
            "request_digest": receipt.get("request_digest") == sha256(normalized_request),
        }
        if receipt_header is not None:
            checks["header_digest"] = (bool(DIGEST.fullmatch(receipt_header))
                                       and receipt_header == receipt.get("digest"))
        return {"schema": "szl.router-verification/v1",
                "status": "CONSISTENT" if all(checks.values()) else "DIVERGENT",
                "checks": checks, "trust": "UNSIGNED_HONEST", "identity_verified": False}
    except (TypeError, ValueError, RecursionError) as exc:
        raise VerificationInputError("verification input is invalid or exceeds bounds") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", help="Bundle JSON file, or '-' for standard input")
    parser.add_argument("--receipt-header", help="Original X-SZL-Receipt value, if retained")
    arguments = parser.parse_args(argv)
    try:
        # Import only after this module initializes; app.py imports hash helpers.
        from router_control.app import ChatRequest
        if arguments.bundle == "-":
            bundle = parse_bundle(sys.stdin.buffer.read(MAX_VERIFICATION_BYTES + 1))
        else:
            with Path(arguments.bundle).open("rb") as handle:
                bundle = parse_bundle(handle.read(MAX_VERIFICATION_BYTES + 1))
        normalized = ChatRequest.model_validate(bundle["request"]).model_dump(mode="json")
        result = verify_completion(bundle["completion"], normalized, arguments.receipt_header)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["status"] == "CONSISTENT" else 1
    except (OSError, ValueError, RecursionError):
        print(json.dumps({"schema": "szl.router-verification/v1", "status": "INVALID_INPUT",
                          "detail": "Expected a bounded completion and valid original chat request."}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
