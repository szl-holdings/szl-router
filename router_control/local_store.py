#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Bounded, read-only byte observation of an Ollama model store.

These hashes bind a sequence of local file reads, not an atomic store snapshot.
They do not attest which bytes an Ollama process loaded or used for a completion.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MAX_MANIFEST_BYTES = 65_536
MAX_BLOB_BYTES = 16_000_000_000
MAX_TOTAL_BLOB_BYTES = 20_000_000_000
MAX_LAYERS = 32
MAX_MANIFEST_NODES = 512
MAX_MANIFEST_DEPTH = 12
HASH_CHUNK_BYTES = 1_048_576
HASH_BUDGET_SECONDS = 20.0
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,191}$")
_REGISTRY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,191}$")
_SHA256_DESCRIPTOR = re.compile(r"^sha256:([0-9a-f]{64})$")
_scan_capacity = threading.BoundedSemaphore(1)


class LocalStoreError(Exception):
    """A sanitized local-store refusal; never contains a filesystem path."""

    def __init__(self, state: str):
        super().__init__(state)
        self.state = state


def manifest_relative_path(model: str) -> Path:
    """Map an Ollama model name to its manifest without accepting a file path."""
    if not model or "\\" in model or model.count(":") > 1:
        raise ValueError("invalid Ollama model name for byte admission")
    parts = model.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise ValueError("invalid Ollama model name for byte admission")
    if "." in parts[0] and len(parts) > 1:
        registry, *repository = parts
        if not _REGISTRY.fullmatch(registry) or ".." in registry:
            raise ValueError("invalid Ollama registry name")
    else:
        registry = "registry.ollama.ai"
        repository = parts if len(parts) > 1 else ["library", parts[0]]
    if len(repository) < 2 or len(repository) > 8:
        raise ValueError("invalid Ollama repository name")
    name, separator, tag = repository[-1].partition(":")
    tag = tag if separator else "latest"
    segments = [*repository[:-1], name, tag]
    if any(not _SEGMENT.fullmatch(part) or part in {".", ".."} for part in segments):
        raise ValueError("invalid Ollama model or tag")
    return Path("manifests", registry, *segments)


def _check_regular_path(root: Path, relative: Path) -> Path:
    """Reject observed symlinks/reparse points and nonregular target files."""
    current = root
    try:
        root_stat = root.lstat()
        if not stat.S_ISDIR(root_stat.st_mode) or _is_reparse(root_stat):
            raise LocalStoreError("LOCAL_STORE_PATH_INVALID")
        for part in relative.parts:
            current = current / part
            info = current.lstat()
            if _is_reparse(info):
                raise LocalStoreError("LOCAL_STORE_PATH_INVALID")
        if not stat.S_ISREG(info.st_mode):
            raise LocalStoreError("LOCAL_STORE_PATH_INVALID")
    except OSError as exc:
        raise LocalStoreError("LOCAL_STORE_FILE_UNAVAILABLE") from exc
    return current


def _is_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _hash_file(root: Path, relative: Path, *, max_bytes: int,
               expected_size: int | None, stop: threading.Event,
               deadline: float) -> tuple[str, bytes | None, int]:
    path = _check_regular_path(root, relative)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_size > max_bytes:
                raise LocalStoreError("LOCAL_STORE_SIZE_INVALID")
            if expected_size is not None and before.st_size != expected_size:
                raise LocalStoreError("LOCAL_STORE_SIZE_MISMATCH")
            digest = hashlib.sha256()
            data = bytearray() if max_bytes == MAX_MANIFEST_BYTES else None
            count = 0
            while True:
                if stop.is_set() or time.monotonic() > deadline:
                    raise LocalStoreError("LOCAL_STORE_CHECK_TIMEOUT")
                chunk = stream.read(min(HASH_CHUNK_BYTES, max_bytes - count + 1))
                if not chunk:
                    break
                count += len(chunk)
                if count > max_bytes:
                    raise LocalStoreError("LOCAL_STORE_SIZE_INVALID")
                digest.update(chunk)
                if data is not None:
                    data.extend(chunk)
            after = os.fstat(stream.fileno())
            fingerprint = lambda info: (
                info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            )
            if count != before.st_size or fingerprint(before) != fingerprint(after):
                raise LocalStoreError("LOCAL_STORE_CHANGED_DURING_READ")
            if expected_size is not None and count != expected_size:
                raise LocalStoreError("LOCAL_STORE_SIZE_MISMATCH")
            return digest.hexdigest(), bytes(data) if data is not None else None, count
    except OSError as exc:
        raise LocalStoreError("LOCAL_STORE_FILE_UNAVAILABLE") from exc


def _parse_manifest(raw: bytes) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("duplicate manifest key")
        return result

    def invalid_constant(_: str) -> None:
        raise ValueError("invalid manifest number")

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                           parse_constant=invalid_constant)
        stack = [(value, 0)]
        nodes = 0
        while stack:
            item, depth = stack.pop()
            nodes += 1
            if depth > MAX_MANIFEST_DEPTH or nodes + len(stack) > MAX_MANIFEST_NODES:
                raise ValueError("manifest structure exceeds bound")
            if isinstance(item, dict):
                stack.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                stack.extend((child, depth + 1) for child in item)
        if not isinstance(value, dict) or value.get("schemaVersion") != 2:
            raise ValueError("invalid manifest schema")
        config, layers = value.get("config"), value.get("layers")
        if not isinstance(config, dict) or not isinstance(layers, list):
            raise ValueError("invalid manifest descriptors")
        if not 1 <= len(layers) <= MAX_LAYERS or not all(isinstance(x, dict) for x in layers):
            raise ValueError("invalid manifest layers")
        return config, layers
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise LocalStoreError("LOCAL_STORE_MANIFEST_INVALID") from exc


def _descriptor(record: dict[str, Any]) -> tuple[str, int, str]:
    digest = record.get("digest")
    size = record.get("size")
    media_type = record.get("mediaType")
    match = _SHA256_DESCRIPTOR.fullmatch(digest) if isinstance(digest, str) else None
    if not match or type(size) is not int or size <= 0 or size > MAX_BLOB_BYTES:
        raise LocalStoreError("LOCAL_STORE_MANIFEST_INVALID")
    if not isinstance(media_type, str) or not media_type.startswith("application/vnd."):
        raise LocalStoreError("LOCAL_STORE_MANIFEST_INVALID")
    return match.group(1), size, media_type


def verify_store_bytes(model: str, expected_manifest: str, expected_weight: str,
                       store_root: str, stop: threading.Event | None = None) -> dict[str, Any]:
    """Hash one manifest and all blobs it names, with one bounded reader."""
    stop = stop or threading.Event()
    if not _scan_capacity.acquire(blocking=False):
        raise LocalStoreError("LOCAL_STORE_CHECK_BUSY")
    try:
        return _verify_store_bytes(model, expected_manifest, expected_weight, store_root, stop)
    finally:
        _scan_capacity.release()


def _verify_store_bytes(model: str, expected_manifest: str, expected_weight: str,
                        store_root: str, stop: threading.Event) -> dict[str, Any]:
    root = Path(store_root)
    if not root.is_absolute():
        raise LocalStoreError("LOCAL_STORE_NOT_CONFIGURED")
    try:
        relative = manifest_relative_path(model)
    except ValueError as exc:
        raise LocalStoreError("LOCAL_STORE_MODEL_NAME_INVALID") from exc
    started_at = datetime.now(timezone.utc).isoformat()
    deadline = time.monotonic() + HASH_BUDGET_SECONDS
    manifest_digest, manifest_data, manifest_size = _hash_file(
        root, relative, max_bytes=MAX_MANIFEST_BYTES, expected_size=None,
        stop=stop, deadline=deadline,
    )
    if manifest_digest != expected_manifest:
        raise LocalStoreError("LOCAL_STORE_MANIFEST_MISMATCH")
    assert manifest_data is not None
    config, layers = _parse_manifest(manifest_data)
    records = [config, *layers]
    model_layers = [record for record in layers
                    if record.get("mediaType") == "application/vnd.ollama.image.model"]
    if len(model_layers) != 1:
        raise LocalStoreError("LOCAL_STORE_MANIFEST_INVALID")
    weight_digest, _, _ = _descriptor(model_layers[0])
    if weight_digest != expected_weight:
        raise LocalStoreError("LOCAL_STORE_WEIGHT_PIN_MISMATCH")
    total_declared = 0
    total_hashed = manifest_size
    for record in records:
        blob_digest, size, _ = _descriptor(record)
        total_declared += size
        if total_declared > MAX_TOTAL_BLOB_BYTES:
            raise LocalStoreError("LOCAL_STORE_SIZE_INVALID")
        observed, _, count = _hash_file(
            root, Path("blobs", "sha256-" + blob_digest), max_bytes=MAX_BLOB_BYTES,
            expected_size=size, stop=stop, deadline=deadline,
        )
        if observed != blob_digest:
            raise LocalStoreError("LOCAL_STORE_BLOB_MISMATCH")
        total_hashed += count
    return {
        "state": "LOCAL_STORE_BYTES_PRE_REQUEST_MATCH",
        "basis": "READ_ONLY_LOCAL_STORE_FILES",
        "observation_scope": "SEQUENTIAL_PRE_REQUEST_READS",
        "observation_started_at": started_at,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "manifest_sha256": manifest_digest,
        "weight_sha256": weight_digest,
        "verified_blob_count": len(records),
        "bytes_hashed": total_hashed,
        "loaded_weight_attestation": "UNAVAILABLE",
        "post_completion_store_check": "NOT_PERFORMED",
    }
