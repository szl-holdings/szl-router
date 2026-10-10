"""Request-wide attempt and deadline budget for one logical router call.

An attempt is an initiated dispatch. Reserving one does not mean the peer
started work, finished it, or cancelled it. A socket timeout is not evidence
that the remote operation stopped.

This object does not reserve money. szl_router.spend_guard.allow() and
record() still do not reserve spend across processes, prove a provider
charge, or enforce a strict USD cap. Unknown price is not treated as free
here; callers that cannot bound a price close the request as cost_unknown
instead of dispatching.

Transport timeouts when a caller uses this budget with the stdlib pool:

- connect and read share one socket timeout, because http.client has no
  separate connect and read deadlines;
- that socket timeout is the minimum of the caller timeout and the time
  remaining until the absolute monotonic deadline;
- the overall deadline is checked again under the budget lock at every
  dispatch boundary, including after backoff. The socket timeout does not
  replace that check.

The ceiling is finite (1..MAX_ATTEMPT_CEILING). Bool, negative, non-finite,
and missing limits are rejected before any dispatch.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Callable, Dict, List, Optional


MAX_ATTEMPT_CEILING = 16

TERMINAL_REASONS = frozenset({
    "success",
    "tests_failed",
    "attempt_exhausted",
    "deadline_exhausted",
    "provider_unavailable",
    "policy_blocked",
    "cost_unknown",
    "cancellation",
})

_IN_FLIGHT = frozenset({"none", "known", "unknown"})


class BudgetStop(Exception):
    """No further dispatch is allowed. The in-flight label is none, known, or unknown."""

    def __init__(self, reason: str, in_flight: str):
        if reason not in TERMINAL_REASONS:
            raise ValueError("unsupported terminal reason")
        if in_flight not in _IN_FLIGHT:
            raise ValueError("in-flight outcome must be none, known, or unknown")
        super().__init__(reason)
        self.reason = reason
        self.in_flight = in_flight


def _label(value: object, name: str) -> str:
    if type(value) is not str or not value or len(value) > 128 or value.strip() != value:
        raise ValueError(f"{name} must be a non-empty string of at most 128 characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(f"{name} must not contain control characters")
    return value


def _finite_real(value: object, name: str) -> float:
    if isinstance(value, bool) or type(value) not in (int, float):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


class RequestBudget:
    """One logical request's attempt counter and absolute monotonic deadline.

    The counter is shared by every provider, adapter, and transport retry that
    receives this same object. Reservation is atomic inside this process. It
    is not a cross-process lease, a durable ledger, or a USD hold.
    """

    def __init__(
        self,
        *,
        request_id: str,
        policy_revision: str,
        max_attempts: int,
        deadline: float,
        clock: Optional[Callable[[], float]] = None,
        sleeper: Optional[Callable[[float], None]] = None,
    ):
        self._request_id = _label(request_id, "request_id")
        self._policy_revision = _label(policy_revision, "policy_revision")
        if type(max_attempts) is not int or not 1 <= max_attempts <= MAX_ATTEMPT_CEILING:
            raise ValueError(
                f"max_attempts must be an int from 1 to {MAX_ATTEMPT_CEILING}"
            )
        self._max_attempts = max_attempts
        self._deadline = _finite_real(deadline, "deadline")
        if clock is None:
            clock = time.monotonic
        if sleeper is None:
            sleeper = time.sleep
        if not callable(clock) or not callable(sleeper):
            raise ValueError("clock and sleeper must be callable")
        self._clock = clock
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self._reserved = 0
        self._cancelled = False
        self._terminal: Optional[str] = None
        self._in_flight = "none"
        self._dispatches: List[Dict[str, object]] = []
        # Reject a broken clock before any caller can dispatch.
        self._read_clock()

    @property
    def request_id(self) -> str:
        return self._request_id

    @property
    def policy_revision(self) -> str:
        return self._policy_revision

    @property
    def max_attempts(self) -> int:
        return self._max_attempts

    @property
    def deadline(self) -> float:
        return self._deadline

    @property
    def reserved(self) -> int:
        with self._lock:
            return self._reserved

    @property
    def terminal(self) -> Optional[str]:
        with self._lock:
            return self._terminal

    @property
    def in_flight(self) -> str:
        with self._lock:
            return self._in_flight

    @property
    def cancellation_requested(self) -> bool:
        with self._lock:
            return self._cancelled

    @property
    def dispatches(self) -> List[Dict[str, object]]:
        with self._lock:
            return [dict(row) for row in self._dispatches]

    def clock(self) -> float:
        return self._read_clock()

    def validate_caller_timeout(self, caller_timeout: object) -> float:
        """Reject a malformed per-dispatch cap before transport is contacted."""
        timeout = _finite_real(caller_timeout, "timeout")
        if timeout <= 0:
            raise ValueError("timeout must be a finite positive number of seconds")
        return timeout

    def reserve(self, caller_timeout: object) -> tuple:
        """Atomically check cancellation and deadline, then reserve one attempt.

        Returns (attempt_number, socket_timeout_seconds). The socket timeout
        is min(caller timeout, remaining deadline). Call this at the dispatch
        boundary, including after backoff or any other wait.
        """
        timeout = self.validate_caller_timeout(caller_timeout)
        with self._lock:
            self._refuse_unlocked()
            now = self._read_clock()
            if now >= self._deadline:
                self._terminal = "deadline_exhausted"
                raise BudgetStop("deadline_exhausted", self._in_flight)
            if self._reserved >= self._max_attempts:
                self._terminal = "attempt_exhausted"
                raise BudgetStop("attempt_exhausted", self._in_flight)
            remain = self._deadline - now
            send_timeout = min(timeout, remain)
            if send_timeout <= 0:
                self._terminal = "deadline_exhausted"
                raise BudgetStop("deadline_exhausted", self._in_flight)
            self._reserved += 1
            attempt = self._reserved
            self._in_flight = "unknown"
            self._dispatches.append({"attempt": attempt, "outcome": "unknown"})
            return attempt, send_timeout

    def note_outcome(self, attempt: int, outcome: str) -> None:
        """Record whether the initiated dispatch's remote outcome is known.

        `known` means a response was observed. `unknown` means the peer may
        still be working. A timeout or dropped socket stays unknown.
        """
        if type(attempt) is not int or attempt < 1:
            raise ValueError("attempt must be a positive int")
        if outcome not in ("known", "unknown"):
            raise ValueError("outcome must be known or unknown")
        with self._lock:
            for row in self._dispatches:
                if row["attempt"] == attempt:
                    row["outcome"] = outcome
                    break
            else:
                raise ValueError("attempt was not reserved")
            self._in_flight = outcome

    def allow_backoff(self, delay: float) -> bool:
        """Return whether a later attempt can still start after `delay` seconds.

        False means do not sleep and do not dispatch. The sleep itself must
        not run past the deadline: remaining time has to be strictly greater
        than the delay so the next reserve still has time to start.
        """
        delay = _finite_real(delay, "backoff")
        if delay < 0:
            raise ValueError("backoff must be nonnegative")
        with self._lock:
            if self._cancelled:
                if self._terminal is None:
                    self._terminal = "cancellation"
                return False
            if self._terminal is not None:
                return False
            if self._reserved >= self._max_attempts:
                self._terminal = "attempt_exhausted"
                return False
            now = self._read_clock()
            remain = self._deadline - now
            if not math.isfinite(remain) or remain <= delay:
                self._terminal = "deadline_exhausted"
                return False
            return True

    def sleep(self, delay: float) -> None:
        """Sleep only after allow_backoff has accepted the same delay."""
        delay = _finite_real(delay, "backoff")
        if delay < 0:
            raise ValueError("backoff must be nonnegative")
        self._sleeper(delay)

    def cancel(self) -> None:
        """Stop later dispatches. An in-flight send is not aborted from here."""
        with self._lock:
            self._cancelled = True
            if self._terminal is None and self._in_flight == "none":
                self._terminal = "cancellation"

    def close(self, reason: str) -> None:
        """Stop later dispatches for a non-success terminal reason.

        Does not claim success. Does not convert an unknown price into a free call.
        """
        if reason not in TERMINAL_REASONS or reason == "success":
            raise ValueError("close() requires a non-success terminal reason")
        with self._lock:
            if self._terminal is None:
                self._terminal = reason

    def mark_success(self) -> None:
        """Record success after a dispatch has already returned a usable result."""
        with self._lock:
            if self._terminal is None:
                self._terminal = "success"

    def _refuse_unlocked(self) -> None:
        if self._terminal is not None:
            raise BudgetStop(self._terminal, self._in_flight)
        if self._cancelled:
            self._terminal = "cancellation"
            raise BudgetStop("cancellation", self._in_flight)

    def _read_clock(self) -> float:
        now = self._clock()
        if isinstance(now, bool) or type(now) not in (int, float) or not math.isfinite(float(now)):
            raise ValueError("clock must return a finite monotonic time")
        return float(now)
