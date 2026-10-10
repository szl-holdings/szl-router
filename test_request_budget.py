"""Offline regressions for the request-wide attempt and deadline budget.

No network, credentials, or real sleeping. The poster is an in-memory stub.
Time moves only when the fake clock is advanced.
"""

from __future__ import annotations

import http.client
import io
import os
import threading
import urllib.error

import pytest

from szl_router import core
from szl_router.request_budget import (
    MAX_ATTEMPT_CEILING,
    BudgetStop,
    RequestBudget,
)


OK = {"choices": [{"message": {"role": "assistant", "content": "ready"}}]}
POLICY = "router-attempt-budget-test/v1"


class Clock:
    def __init__(self, now: float = 0.0):
        self.now = now
        self.slept = []

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _budget(clock: Clock, *, attempts: int = 4, deadline: float = 100.0,
            request_id: str = "req-budget-1") -> RequestBudget:
    return RequestBudget(
        request_id=request_id,
        policy_revision=POLICY,
        max_attempts=attempts,
        deadline=deadline,
        clock=clock,
        sleeper=clock.sleep,
    )


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://example.invalid/v1/chat/completions", code, "boom", {},
        io.BytesIO(b'{"error":"boom"}'),
    )


class _Routes:
    def __init__(self, specs):
        self.calls = []
        self._old_providers = core.PROVIDERS
        self._had_route = "budget-chat" in core.MODEL_ROUTES
        self._old_route = core.MODEL_ROUTES.get("budget-chat")
        self._old_cd = os.environ.get("SZL_COOLDOWN_SECONDS")
        self._old_until = dict(core._COOLDOWN_UNTIL)
        self._old_post = core._post_chat
        providers = {}
        routes = []
        for name, sovereign in specs:
            providers[name] = core.Provider(
                name, "", f"http://{name}.invalid/v1", "", sovereign,
                "self-hosted" if sovereign else "grid",
            )
            routes.append((name, "budget-model"))
        core.PROVIDERS = providers
        core.MODEL_ROUTES["budget-chat"] = routes
        os.environ["SZL_COOLDOWN_SECONDS"] = "0"
        core._COOLDOWN_UNTIL.clear()

    def install(self, poster):
        def post(provider, payload, timeout):
            self.calls.append((provider.name, timeout))
            return poster(provider, payload, timeout)
        core._post_chat = post

    def close(self):
        core._post_chat = self._old_post
        core.PROVIDERS = self._old_providers
        if self._had_route:
            core.MODEL_ROUTES["budget-chat"] = self._old_route
        else:
            core.MODEL_ROUTES.pop("budget-chat", None)
        core._COOLDOWN_UNTIL.clear()
        core._COOLDOWN_UNTIL.update(self._old_until)
        if self._old_cd is None:
            os.environ.pop("SZL_COOLDOWN_SECONDS", None)
        else:
            os.environ["SZL_COOLDOWN_SECONDS"] = self._old_cd


def _dispatch_rows(attempts):
    skips = ("provider unavailable", "pricing/", "cooldown-skip")
    return [row for row in attempts if not (row.error or "").startswith(skips)]


def test_retry_and_fallback_share_one_limit():
    clock = Clock()
    budget = _budget(clock, attempts=3, deadline=50)
    routes = _Routes((("first", True), ("second", True)))
    try:
        def poster(provider, payload, timeout):
            if provider.name == "second":
                return dict(OK)
            raise _http_error(503)
        routes.install(poster)
        with pytest.raises(core.RouterError) as caught:
            core.chat("budget-chat", [{"role": "user", "content": "hi"}],
                      timeout=30, request_budget=budget)
    finally:
        routes.close()
    assert [name for name, _timeout in routes.calls] == ["first", "first", "first"]
    assert budget.reserved == 3
    assert budget.terminal == "attempt_exhausted"
    assert caught.value.terminal_reason == "attempt_exhausted"
    assert len(caught.value.attempts) == 3
    assert clock.slept  # backoff happened between the shared attempts
    assert all(delay < 50 for delay in clock.slept)


def test_permanent_failure_can_fall_through_inside_the_same_ceiling():
    clock = Clock()
    budget = _budget(clock, attempts=2, deadline=50)
    routes = _Routes((("first", True), ("second", True)))
    try:
        def poster(provider, payload, timeout):
            if provider.name == "first":
                raise _http_error(400)
            return dict(OK)
        routes.install(poster)
        result = core.chat("budget-chat", [{"role": "user", "content": "hi"}],
                           timeout=30, request_budget=budget)
    finally:
        routes.close()
    assert [name for name, _timeout in routes.calls] == ["first", "second"]
    assert budget.reserved == 2
    assert budget.terminal == "success"
    assert result["x_szl_provenance"]["terminal_reason"] == "success"
    assert result["x_szl_provenance"]["attempts_reserved"] == 2
    assert "lambda" not in result["x_szl_provenance"]


def test_late_timeout_does_not_dispatch_again():
    clock = Clock()
    budget = _budget(clock, attempts=5, deadline=5)
    seen = []
    routes = _Routes((("first", True), ("second", True)))
    try:
        def poster(provider, payload, timeout):
            seen.append(timeout)
            clock.advance(5)  # the socket timeout lands on the request deadline
            raise TimeoutError("timed out")
        routes.install(poster)
        with pytest.raises(core.RouterError) as caught:
            core.chat("budget-chat", [{"role": "user", "content": "hi"}],
                      timeout=30, request_budget=budget)
    finally:
        routes.close()
    assert seen == [5.0]
    assert [name for name, _timeout in routes.calls] == ["first"]
    assert budget.terminal == "deadline_exhausted"
    assert budget.in_flight == "unknown"
    assert caught.value.terminal_reason == "deadline_exhausted"
    assert clock.slept == []


def test_backoff_cannot_outlive_the_request():
    clock = Clock()
    budget = _budget(clock, attempts=4, deadline=1)
    routes = _Routes((("first", True), ("second", True)))
    original = core._backoff_sleep_seconds
    core._backoff_sleep_seconds = lambda _attempt: 5.0
    try:
        def poster(provider, payload, timeout):
            raise _http_error(503)
        routes.install(poster)
        with pytest.raises(core.RouterError) as caught:
            core.chat("budget-chat", [{"role": "user", "content": "hi"}],
                      timeout=30, request_budget=budget)
    finally:
        core._backoff_sleep_seconds = original
        routes.close()
    assert [name for name, _timeout in routes.calls] == ["first"]
    assert clock.slept == []
    assert budget.terminal == "deadline_exhausted"
    assert caught.value.terminal_reason == "deadline_exhausted"


def test_invalid_limits_stop_before_transport():
    clock = Clock()
    calls = []

    def boom(*_args, **_kwargs):
        calls.append("transport")
        raise AssertionError("transport was called")

    bad_attempts = (True, False, -1, 0, 1.5, None, 10 ** 9, MAX_ATTEMPT_CEILING + 1)
    for value in bad_attempts:
        with pytest.raises(ValueError):
            RequestBudget(
                request_id="req-bad", policy_revision=POLICY, max_attempts=value,
                deadline=10, clock=clock, sleeper=clock.sleep,
            )
    for deadline in (True, None, float("nan"), float("inf"), float("-inf"), "10"):
        with pytest.raises(ValueError):
            RequestBudget(
                request_id="req-bad", policy_revision=POLICY, max_attempts=1,
                deadline=deadline, clock=clock, sleeper=clock.sleep,
            )
    budget = _budget(clock, attempts=2, deadline=10)
    routes = _Routes((("first", True),))
    try:
        routes.install(boom)
        for timeout in (True, 0, -1, float("nan"), float("inf")):
            with pytest.raises(ValueError):
                core.chat("budget-chat", [{"role": "user", "content": "hi"}],
                          timeout=timeout, request_budget=budget)
    finally:
        routes.close()
    assert calls == []
    assert routes.calls == []
    assert budget.reserved == 0


def test_cancellation_stops_later_dispatch():
    clock = Clock()
    before = _budget(clock, attempts=4, deadline=30)
    before.cancel()
    routes = _Routes((("first", True), ("second", True)))
    try:
        def poster(provider, payload, timeout):
            raise AssertionError("cancelled request dispatched")
        routes.install(poster)
        with pytest.raises(core.RouterError) as caught:
            core.chat("budget-chat", [{"role": "user", "content": "hi"}],
                      timeout=5, request_budget=before)
        assert routes.calls == []
        assert caught.value.terminal_reason == "cancellation"
        assert before.in_flight == "none"

        during = _budget(clock, attempts=4, deadline=30, request_id="req-budget-2")
        def cancel_during(provider, payload, timeout):
            during.cancel()
            raise _http_error(503)
        routes.install(cancel_during)
        with pytest.raises(core.RouterError) as caught_during:
            core.chat("budget-chat", [{"role": "user", "content": "hi"}],
                      timeout=5, request_budget=during)
    finally:
        routes.close()
    assert [name for name, _timeout in routes.calls] == ["first"]
    assert during.reserved == 1
    assert during.terminal == "cancellation"
    # HTTP 503 is a response from the peer, so the in-flight outcome is known.
    # Cancellation still forbids another dispatch. A timeout stays unknown;
    # that case is test_late_timeout_does_not_dispatch_again.
    assert during.in_flight == "known"
    assert caught_during.value.terminal_reason == "cancellation"
    assert clock.slept == []


def test_receipt_attempts_match_actual_calls():
    clock = Clock()
    budget = _budget(clock, attempts=3, deadline=40)
    routes = _Routes((("moonshot", False), ("first", True), ("second", True)))
    try:
        def poster(provider, payload, timeout):
            if provider.name == "first":
                failures = sum(1 for name, _timeout in routes.calls if name == "first")
                # One retryable failure, then a permanent failure, then fallback.
                raise _http_error(503 if failures == 1 else 400)
            return dict(OK)
        routes.install(poster)
        result = core.chat("budget-chat", [{"role": "user", "content": "hi"}],
                           timeout=10, request_budget=budget)
    finally:
        routes.close()
    names = [name for name, _timeout in routes.calls]
    assert names == ["first", "first", "second"]
    assert "moonshot" not in names
    provenance = result["x_szl_provenance"]
    assert provenance["request_id"] == "req-budget-1"
    assert provenance["policy_revision"] == POLICY
    assert provenance["terminal_reason"] == "success"
    assert provenance["attempts_reserved"] == budget.reserved == 3
    attempt_rows = provenance["attempts"]
    transport_rows = _dispatch_rows([
        core.Attempt(row["provider"], row["upstream_model"], row["ok"],
                     status=row["status"], error=row["error"])
        for row in attempt_rows
    ])
    assert len(transport_rows) == len(names) == len(budget.dispatches)
    assert [row["outcome"] for row in budget.dispatches] == ["known", "known", "known"]
    assert attempt_rows[0]["error"].startswith("pricing/")


def test_policy_block_does_not_dispatch_or_weaken_qualification():
    clock = Clock()
    budget = _budget(clock, attempts=3, deadline=20)
    routes = _Routes((("groq", False), ("moonshot", False)))
    try:
        def poster(provider, payload, timeout):
            raise AssertionError("unqualified route was dispatched")
        routes.install(poster)
        with pytest.raises(core.RouterError) as caught:
            core.chat("budget-chat", [{"role": "user", "content": "hi"}],
                      timeout=5, request_budget=budget)
    finally:
        routes.close()
    assert routes.calls == []
    assert budget.reserved == 0
    assert caught.value.terminal_reason == "policy_blocked"
    assert budget.terminal == "policy_blocked"


def test_controller_cost_unknown_and_tests_failed_stop_before_dispatch():
    clock = Clock()
    routes = _Routes((("first", True),))
    try:
        def poster(provider, payload, timeout):
            raise AssertionError("closed budget dispatched")
        routes.install(poster)
        for reason in ("cost_unknown", "tests_failed"):
            budget = _budget(clock, attempts=3, deadline=20, request_id="req-" + reason)
            budget.close(reason)
            with pytest.raises(core.RouterError) as caught:
                core.chat("budget-chat", [{"role": "user", "content": "hi"}],
                          timeout=5, request_budget=budget)
            assert caught.value.terminal_reason == reason
            assert budget.reserved == 0
    finally:
        routes.close()
    assert routes.calls == []


def test_budget_disables_uncounted_pool_retry():
    sends = []

    class FakeConn:
        def __init__(self):
            self.timeout = None
            self.sock = None

        def request(self, method, path, body=None, headers=None):
            sends.append(path)
            raise http.client.HTTPException("stale pooled connection")

        def close(self):
            return None

    pool = core._ConnectionPool()
    pool._checkout = lambda *_args, **_kwargs: FakeConn()
    pool._new_conn = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("hidden second connection")
    )
    token = core._REQUEST_BUDGET.set(object())
    try:
        with pytest.raises(http.client.HTTPException):
            pool.request_json("http://example.invalid/v1/chat/completions", b"{}", {}, 1.0)
    finally:
        core._REQUEST_BUDGET.reset(token)
    assert sends == ["/v1/chat/completions"]


def test_unbudgeted_pool_still_retries_a_stale_connection_once():
    sends = []

    class FakeResp:
        status = 200
        reason = "OK"
        version = 11

        def read(self):
            return b'{"ok":true}'

        def getheader(self, _name):
            return "close"

        def getheaders(self):
            return []

    class FakeConn:
        def __init__(self, fail):
            self.fail = fail
            self.timeout = None
            self.sock = None

        def request(self, method, path, body=None, headers=None):
            sends.append(self.fail)
            if self.fail:
                raise OSError("stale")

        def getresponse(self):
            return FakeResp()

        def close(self):
            return None

    pool = core._ConnectionPool()
    pool._checkout = lambda *_args, **_kwargs: FakeConn(True)
    pool._new_conn = lambda *_args, **_kwargs: FakeConn(False)
    assert core._REQUEST_BUDGET.get() is None
    body = pool.request_json("http://example.invalid/v1/chat/completions", b"{}", {}, 1.0)
    assert body == {"ok": True}
    assert sends == [True, False]


def test_warm_connection_applies_the_capped_socket_timeout():
    class Sock:
        def __init__(self):
            self.timeouts = []

        def settimeout(self, value):
            self.timeouts.append(value)

    class Conn:
        def __init__(self):
            self.timeout = 30.0
            self.sock = Sock()

        def close(self):
            return None

    pool = core._ConnectionPool()
    conn = Conn()
    pool._idle[("http", "example.invalid", 80)] = [conn]
    checked = pool._checkout("http", "example.invalid", 80, 0.25)
    assert checked is conn
    assert conn.timeout == 0.25
    assert conn.sock.timeouts == [0.25]


def test_reserve_is_atomic_for_one_attempt():
    clock = Clock()
    budget = _budget(clock, attempts=1, deadline=10)
    results = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        try:
            results.append(("reserved", budget.reserve(1.0)[0]))
        except BudgetStop as stopped:
            results.append((stopped.reason, stopped.in_flight))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert [item for item in results if item[0] == "reserved"] == [("reserved", 1)]
    assert sum(1 for kind, _value in results if kind == "reserved") == 1
    assert budget.reserved == 1
    assert all(kind == "attempt_exhausted" for kind, _value in results if kind != "reserved")


def test_direct_retry_uses_the_same_budget_object():
    clock = Clock()
    budget = _budget(clock, attempts=2, deadline=10)
    calls = []

    def poster(_provider, _payload, timeout):
        calls.append(timeout)
        raise _http_error(429)

    with pytest.raises(urllib.error.HTTPError):
        core._post_with_retry(poster, None, {"model": "m"}, 9, budget=budget)
    assert calls == [9.0, 9.0]
    assert budget.reserved == 2
    assert budget.terminal == "attempt_exhausted"
    assert len(budget.dispatches) == 2


VECTOR = {"data": [{"index": 0, "embedding": [0.25, 0.5]}]}


class _EmbedRoutes:
    def __init__(self, specs):
        self.calls = []
        self._old_providers = core.PROVIDERS
        self._had_route = "budget-embed" in core.EMBED_ROUTES
        self._old_route = core.EMBED_ROUTES.get("budget-embed")
        self._old_post = core._post_embeddings
        providers = {}
        routes = []
        for name, sovereign in specs:
            providers[name] = core.Provider(
                name, "", f"http://{name}.invalid/v1", "", sovereign,
                "self-hosted" if sovereign else "grid",
            )
            routes.append((name, "budget-embed-model"))
        core.PROVIDERS = providers
        core.EMBED_ROUTES["budget-embed"] = routes

    def install(self, poster):
        def post(provider, payload, timeout):
            self.calls.append((provider.name, timeout))
            return poster(provider, payload, timeout)
        core._post_embeddings = post

    def close(self):
        core._post_embeddings = self._old_post
        core.PROVIDERS = self._old_providers
        if self._had_route:
            core.EMBED_ROUTES["budget-embed"] = self._old_route
        else:
            core.EMBED_ROUTES.pop("budget-embed", None)


def test_embed_retry_and_fallback_share_one_limit():
    clock = Clock()
    budget = _budget(clock, attempts=2, deadline=40)
    routes = _EmbedRoutes((("first", True), ("second", True)))
    try:
        def poster(provider, payload, timeout):
            raise _http_error(503)
        routes.install(poster)
        with pytest.raises(core.RouterError) as caught:
            core.embed("budget-embed", "hello", timeout=20, use_cache=False,
                       request_budget=budget)
    finally:
        routes.close()
    assert [name for name, _timeout in routes.calls] == ["first", "first"]
    assert budget.reserved == 2
    assert budget.terminal == "attempt_exhausted"
    assert caught.value.terminal_reason == "attempt_exhausted"


def test_embed_fallback_stays_inside_the_shared_ceiling():
    clock = Clock()
    budget = _budget(clock, attempts=2, deadline=40)
    routes = _EmbedRoutes((("first", True), ("second", True)))
    try:
        def poster(provider, payload, timeout):
            if provider.name == "first":
                raise _http_error(400)
            return dict(VECTOR)
        routes.install(poster)
        result = core.embed("budget-embed", "hello", timeout=20, use_cache=False,
                            request_budget=budget)
    finally:
        routes.close()
    assert [name for name, _timeout in routes.calls] == ["first", "second"]
    assert budget.reserved == 2
    assert budget.terminal == "success"
    assert result["x_szl_provenance"]["terminal_reason"] == "success"
    assert result["x_szl_provenance"]["attempts_reserved"] == 2


def test_embed_cache_hit_does_not_reserve_an_attempt():
    clock = Clock()
    budget = _budget(clock, attempts=2, deadline=40)
    routes = _EmbedRoutes((("first", True),))
    try:
        routes.install(lambda provider, payload, timeout: dict(VECTOR))
        first = core.embed("budget-embed", "cached-text", timeout=20,
                           request_budget=budget)
        assert budget.reserved == 1
        assert budget.terminal == "success"
        second_budget = _budget(clock, attempts=2, deadline=40, request_id="req-cache")
        second = core.embed("budget-embed", "cached-text", timeout=20,
                            request_budget=second_budget)
    finally:
        routes.close()
    assert second["x_szl_provenance"]["served_by"].endswith(":cache")
    assert second_budget.reserved == 0
    assert second_budget.terminal is None
    assert first["data"][0]["embedding"] == [0.25, 0.5]


def test_embed_rejects_a_non_budget_before_transport():
    routes = _EmbedRoutes((("first", True),))
    try:
        routes.install(lambda provider, payload, timeout: dict(VECTOR))
        with pytest.raises(TypeError):
            core.embed("budget-embed", "hello", use_cache=False, request_budget=True)
    finally:
        routes.close()
    assert routes.calls == []
