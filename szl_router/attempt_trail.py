"""Inert, disclosure-only telemetry trail; direct statistics first.

2026-11-20 three-brain study consensus (szl-router estate doctrine): this
module is the Phase 2 evidence surface for provider_scorecard.py. It appends
and reads local JSONL attempt records so the scorecard can compute DIRECT
STATISTICS from real recorded evidence. Estimates are never qualification;
the trail qualifies nothing, arms nothing, and routes nothing.

No network, no keys, no secrets: provider/model are plain identifiers and
must never carry credentials. The trail spends nothing. Pure stdlib.

Fail-closed policy, stated plainly: corrupt lines (invalid JSON, wrong
schema, invalid UTF-8, pathologically nested or oversize lines) are skipped
and counted, never repaired or guessed; a missing file is no evidence.
Appends are single-syscall binary writes (single-writer assumption; the
reader counts any torn line as corrupt rather than inventing data). Kept
records are materialized in memory on read. I/O errors other than a
missing file (permissions, vanished directory, mid-read failure) propagate
loudly: failure is disclosed, never invented.
"""
from __future__ import annotations

import json
import math
import os
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

_SCHEMA = "szl.router.attempt_trail/v1"
_NOTE = "direct records only; corrupt lines skipped and counted, never repaired"
_KEYS = frozenset(("provider", "model", "ok", "latency_s", "ts"))
_MAX_LINE_BYTES = 1 << 20  # 1 MiB cap: a legitimate attempt record is < 1 KB


def _valid_identifier(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value.splitlines() == [value]
    )


def _finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        return False


def _finite_float(value: Any, field: str) -> float:
    if not _finite_number(value):
        raise ValueError(field + " must be a finite float")
    return float(value)


def _disclosure(trail_path: str, lines: int, kept: int, corrupt: int) -> Dict[str, Any]:
    return {
        "schema": _SCHEMA,
        "path_basename": os.path.basename(trail_path),
        "lines": lines,
        "kept": kept,
        "corrupt": corrupt,
        "note": _NOTE,
    }


def _parse_record(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict) or set(value) != _KEYS:
        return None

    provider = value["provider"]
    model = value["model"]
    ok = value["ok"]
    latency_s = value["latency_s"]
    ts = value["ts"]

    if not _valid_identifier(provider) or not _valid_identifier(model):
        return None
    if type(ok) is not bool:
        return None
    if latency_s is not None:
        if not _finite_number(latency_s):
            return None
        latency_s = float(latency_s)
    if not _finite_number(ts):
        return None

    return {
        "provider": provider,
        "model": model,
        "ok": ok,
        "latency_s": latency_s,
        "ts": float(ts),
    }


def _to_attempts(
    records: Sequence[Dict[str, Any]],
) -> List[Tuple[str, str, bool, Optional[float], float]]:
    return [
        (item["provider"], item["model"], item["ok"],
         item["latency_s"], item["ts"])
        for item in records
    ]


def record(
    trail_path: str,
    provider: str,
    model: str,
    ok: bool,
    latency_s: Optional[float],
    ts: Optional[float] = None,
) -> Dict[str, Any]:
    """Append one attempt; provider/model are identifiers, never secrets."""
    if not _valid_identifier(provider):
        raise ValueError("provider must be non-empty and contain no line breaks")
    if not _valid_identifier(model):
        raise ValueError("model must be non-empty and contain no line breaks")
    if type(ok) is not bool:
        raise ValueError("ok must be a bool")

    latency = None if latency_s is None else _finite_float(latency_s, "latency_s")
    resolved_ts = time.time() if ts is None else ts
    resolved_ts = _finite_float(resolved_ts, "ts")

    item: Dict[str, Any] = {
        "provider": provider,
        "model": model,
        "ok": ok,
        "latency_s": latency,
        "ts": resolved_ts,
    }
    line = json.dumps(item, sort_keys=True, separators=(",", ":"), allow_nan=False)
    payload = (line + "\n").encode("utf-8")
    parent = os.path.dirname(os.path.abspath(trail_path))
    os.makedirs(parent, exist_ok=True)
    # Single-syscall binary append: no text-mode newline translation (the
    # on-disk bytes are identical on every platform), and one os.write per
    # record keeps concurrent torn-line risk minimal under the documented
    # single-writer assumption. Any torn line is corrupt to the reader,
    # never invented data.
    fd = os.open(trail_path,
                 os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        view = memoryview(payload)
        while view:
            view = view[os.write(fd, view):]
    finally:
        os.close(fd)
    return item


def read(trail_path: str) -> Dict[str, Any]:
    """Fail-closed read: missing file is no evidence, corrupt lines counted."""
    records: List[Dict[str, Any]] = []
    lines = 0
    corrupt = 0

    try:
        # Binary line iteration + per-line decode: a line with invalid UTF-8
        # must count as corrupt, never crash the whole read.
        handle = open(trail_path, "rb")
    except FileNotFoundError:
        return {"records": [], "disclosure": _disclosure(trail_path, 0, 0, 0)}

    with handle:
        for raw_line in handle:
            lines += 1
            # Bounds: oversize and pathologically nested lines are corrupt,
            # never a crash (RecursionError from json bombs) or OOM guesses.
            if len(raw_line) > _MAX_LINE_BYTES:
                corrupt += 1
                continue
            try:
                value = json.loads(raw_line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError, RecursionError):
                corrupt += 1
                continue
            item = _parse_record(value)
            if item is None:
                corrupt += 1
            else:
                records.append(item)

    return {
        "records": records,
        "disclosure": _disclosure(trail_path, lines, len(records), corrupt),
    }


def scorecard_from_trail(
    trail_path: str,
    *,
    window_s: Optional[float] = None,
    min_samples: int = 30,
) -> Dict[str, Any]:
    """Score the trail through provider_scorecard; never invent records."""
    payload = read(trail_path)
    attempts = _to_attempts(payload["records"])

    from szl_router import provider_scorecard  # lazy: no import cycle

    output = dict(provider_scorecard.scorecard(
        attempts, window_s=window_s, min_samples=min_samples))
    disclosure = payload["disclosure"]
    output["trail"] = {
        "lines": disclosure["lines"],
        "kept": disclosure["kept"],
        "corrupt": disclosure["corrupt"],
    }
    return output