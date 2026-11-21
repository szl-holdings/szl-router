"""Provider hints are request-local. No upstream traffic or real timers."""

import io
from datetime import datetime, timezone
from email.message import Message
from http.client import HTTPResponse
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest

from szl_router import core


def _error(status, value):
    return HTTPError("http://provider.invalid/", status, "upstream",
                     {"rEtRy-AfTeR": value}, io.BytesIO(b"original error"))


@pytest.mark.parametrize(("value", "expected"), [
    ("2", 2.0),
    ("0", 0.25),
    (" 0002 ", 2.0),
    ("Thu, 01 Jan 1970 00:00:03 GMT", 3.0),
    ("Wednesday, 31-Dec-69 23:59:59 GMT", 0.25),
    ("Thu Jan  1 00:00:02 1970", 2.0),
    ("not a date", 0.25),
    ("Thu, 01 Jan 1970 00:00:03 +0000", 0.25),
    ("Thu, 01 Jan 1970 00:00:03 GMT garbage", 0.25),
    ("", 0.25),
    ("-1", 0.25),
    ("1.25", 0.25),
    ("inf", 0.25),
    ("4", 4.0),
    ("5", None),
    ("9" * 5000, None),
])
def test_hint_and_local_jitter(monkeypatch, value, expected):
    monkeypatch.setattr(core.time, "time", lambda: 0)
    monkeypatch.setattr(core, "_RETRY_MAX_DELAY", 4.0)
    monkeypatch.setattr(core, "_backoff_sleep_seconds", lambda i: 0.25)
    exc = _error(429, value)
    assert core._retry_delay(0, exc) == expected
    assert exc.read() == b"original error"


def test_header_does_not_ask_for_unbounded_or_ambiguous_wait(monkeypatch):
    monkeypatch.setattr(core, "_RETRY_MAX_DELAY", 100.0)
    monkeypatch.setattr(core, "_backoff_sleep_seconds", lambda i: 0.25)
    exc = _error(503, "61")
    assert core._retry_delay(0, exc) is None
    exc.headers = Message()
    exc.headers["Retry-After"] = "2"
    assert core._retry_delay(0, exc) == 2.0
    exc.headers["Retry-After"] = "3"
    assert core._retry_delay(0, exc) == 0.25
    assert core._retry_delay(0, _error(500, "2")) == 0.25
    assert core._retry_delay(0, OSError("connection dropped")) == 0.25


def test_rfc850_date_uses_http_sliding_century(monkeypatch):
    now = datetime(2070, 1, 1, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(core.time, "time", lambda: now)
    assert core._retry_after_seconds(_error(503, "Wednesday, 01-Jan-70 00:00:02 GMT")) == 2.0


@pytest.mark.parametrize("status", [429, 503])
def test_legacy_loop_waits_once_for_same_provider(monkeypatch, status):
    slept, calls = [], []
    monkeypatch.setattr(core.time, "sleep", slept.append)
    monkeypatch.setattr(core, "_RETRY_MAX_DELAY", 4.0)
    monkeypatch.setattr(core, "_RETRY_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(core, "_backoff_sleep_seconds", lambda i: 0.25)

    def post(*_):
        calls.append(1)
        if len(calls) == 1:
            raise _error(status, "3")
        return {"ok": True}

    assert core._post_with_retry(post, None, {}, 1.0) == {"ok": True}
    assert len(calls) == 2
    assert slept == [3.0]


def test_legacy_loop_rethrows_excessive_hint_without_sleep_or_retry(monkeypatch):
    calls = []
    exc = _error(429, "999999")
    monkeypatch.setattr(core, "_RETRY_MAX_DELAY", 4.0)
    monkeypatch.setattr(core.time, "sleep", lambda _: pytest.fail("unexpected wait"))

    def post(*_):
        calls.append(1)
        raise exc

    with pytest.raises(HTTPError) as caught:
        core._post_with_retry(post, None, {}, 1.0)
    assert caught.value is exc
    assert len(calls) == 1


def test_pool_keeps_duplicate_retry_headers_and_error_body(monkeypatch):
    stream = io.BytesIO(b"HTTP/1.1 503 Busy\r\nRetry-After: 2\r\n"
                        b"retry-after: 3\r\nContent-Length: 4\r\n\r\nbusy")
    sock = Mock()
    sock.makefile.return_value = stream
    response = HTTPResponse(sock)
    response.begin()
    conn = Mock()
    conn.getresponse.return_value = response
    pool = core._ConnectionPool()
    monkeypatch.setattr(pool, "_checkout", lambda *_: conn)
    with pytest.raises(HTTPError) as caught:
        pool.request_json("http://provider.invalid/v1", b"{}", {}, 1.0)
    assert caught.value.headers.get_all("Retry-After") == ["2", "3"]
    assert core._retry_after_seconds(caught.value) is None
    assert caught.value.read() == b"busy"
