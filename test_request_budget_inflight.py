"""Request-wide uncertainty survives later responses from other dispatches."""

from __future__ import annotations

import pytest

from szl_router import core
from szl_router.request_budget import BudgetStop, RequestBudget


def budget() -> RequestBudget:
    return RequestBudget(
        request_id="unknown-earlier-dispatch",
        policy_revision="request-budget-regression/v1",
        max_attempts=2,
        deadline=100.0,
        clock=lambda: 0.0,
        sleeper=lambda _delay: None,
    )


def test_timeout_then_response_preserves_unknown_on_exhaustion():
    request = budget()
    first, _ = request.reserve(1.0)
    request.note_outcome(first, "unknown")
    second, _ = request.reserve(1.0)
    request.note_outcome(second, "known")

    assert request.dispatches == [
        {"attempt": 1, "outcome": "unknown"},
        {"attempt": 2, "outcome": "known"},
    ]
    with pytest.raises(BudgetStop) as stopped:
        request.reserve(1.0)
    assert stopped.value.reason == "attempt_exhausted"
    assert stopped.value.in_flight == "unknown"
    assert request.in_flight == "unknown"


@pytest.mark.parametrize("first_response", [1, 2])
def test_response_to_one_concurrent_dispatch_does_not_resolve_the_other(first_response):
    request = budget()
    assert request.in_flight == "none"
    request.reserve(1.0)
    request.reserve(1.0)

    request.note_outcome(first_response, "known")
    assert request.in_flight == "unknown"
    request.note_outcome(3 - first_response, "known")
    assert request.in_flight == "known"


def test_successful_retry_retains_uncertainty_about_timed_out_dispatch():
    request = budget()
    attempts = []
    expected = {"choices": [{"message": {"content": "ready"}}]}

    def poster(_provider, _payload, timeout):
        attempts.append(timeout)
        if len(attempts) == 1:
            raise TimeoutError("remote operation may still be running")
        return expected

    result = core._post_with_retry(poster, None, {}, 1.0, budget=request)
    request.mark_success()

    assert result is expected
    assert attempts == [1.0, 1.0]
    assert request.terminal == "success"
    assert request.in_flight == "unknown"
    assert [row["outcome"] for row in request.dispatches] == ["unknown", "known"]
