"""Trust/resource-boundary tests for the legacy SZL Mesh Coordinator (RQ-20).

Fake transports only: the coordinator's upstream HTTP client is replaced by an
in-memory fake that records headers. No real provider, no real credential, no
network beyond a loopback test socket. Token values below are obvious fakes.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import threading
from http.server import ThreadingHTTPServer

import pytest

sys.path.insert(0, "szl_router")
import mesh_coordinator as mc  # noqa: E402

FAKE_CALLER = "fake-caller-token-for-tests"
FAKE_UPSTREAM = "fake-upstream-key-for-tests"

# Keep the real loopback client: fake_upstream patches the shared http.client module.
CLIENT = http.client.HTTPConnection


class _FakeResp:
    status = 200

    def read(self):
        return b'{"choices":[{"message":{"content":"ok"}}]}'

    def getheaders(self):
        return [("Content-Type", "application/json")]


@pytest.fixture
def fake_upstream(monkeypatch):
    sent = []

    class FakeConn:
        def __init__(self, host, port, timeout=None):
            assert (host, port) == ("omen.example", 11434), "fake intercepted a non-upstream connection"
            self.host = host
            self.port = port

        def request(self, method, path, body=None, headers=None):
            sent.append({"host": self.host, "port": self.port, "path": path, "headers": dict(headers or {}),
                         "body": body})

        def getresponse(self):
            return _FakeResp()

        def close(self):
            pass

    monkeypatch.setattr(mc.http.client, "HTTPConnection", FakeConn)
    monkeypatch.setattr(mc.http.client, "HTTPSConnection", FakeConn)
    monkeypatch.setenv("SZL_MESH_OMEN_TOKEN", FAKE_UPSTREAM)
    return sent


def _coord():
    w = mc.Worker(name="omen", base_url="http://omen.example:11434/v1", sovereign=True)
    w.reachable = True
    c = mc.MeshCoordinator([w])
    c.refresh_reachability = lambda force=False: None  # type: ignore
    return c


@pytest.fixture
def server(request):
    servers = []

    def start(**kw):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), mc.make_handler(_coord(), **kw))
        srv.daemon_threads = True
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        servers.append(srv)
        return srv.server_address[1]

    yield start
    for s in servers:
        s.shutdown()
        s.server_close()


def _raw(port, data: bytes, half_close: bool = False, timeout: float = 5.0):
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(data)
        if half_close:
            s.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            try:
                b = s.recv(65536)
            except socket.timeout:
                break
            if not b:
                break
            chunks.append(b)
    head = b"".join(chunks).split(b"\r\n", 1)[0].decode()
    return int(head.split()[1])


def _post(port, headers, body=b"{}"):
    conn = CLIENT("127.0.0.1", port, timeout=5)
    conn.request("POST", "/v1/chat/completions", body=body, headers=headers)
    r = conn.getresponse()
    r.read()
    conn.close()
    return r.status


def test_default_bind_is_loopback():
    assert mc.DEFAULT_HOST == "127.0.0.1"
    import inspect
    assert inspect.signature(mc.serve).parameters["host"].default == "127.0.0.1"
    src = open("szl_router/mesh_coordinator.py", encoding="utf-8").read()
    assert '"0.0.0.0"' not in src


def test_non_loopback_bind_refused_without_opt_in_and_token():
    with pytest.raises(mc.CoordinatorConfigError):
        mc.check_bind("0.0.0.0", None, False)
    with pytest.raises(mc.CoordinatorConfigError):
        mc.check_bind("0.0.0.0", FAKE_CALLER, False)  # token but no opt-in
    with pytest.raises(mc.CoordinatorConfigError):
        mc.check_bind("10.1.2.3", None, True)  # opt-in but no token
    mc.check_bind("0.0.0.0", FAKE_CALLER, True)  # both -> allowed
    for h in ("127.0.0.1", "localhost", "::1", "[::1]", "127.0.0.2"):
        mc.check_bind(h, None, False)


def test_serve_refuses_before_binding(monkeypatch):
    monkeypatch.delenv(mc.CALLER_TOKEN_ENV, raising=False)
    monkeypatch.delenv(mc.ALLOW_NON_LOOPBACK_ENV, raising=False)
    bound = []
    monkeypatch.setattr(mc, "ThreadingHTTPServer", lambda *a, **k: bound.append(a))
    with pytest.raises(mc.CoordinatorConfigError):
        mc.serve("0.0.0.0", 0)
    monkeypatch.setenv(mc.ALLOW_NON_LOOPBACK_ENV, "1")
    with pytest.raises(mc.CoordinatorConfigError):
        mc.serve("0.0.0.0", 0)
    assert bound == []


def test_caller_authorized_constant_time_helper():
    assert mc.caller_authorized("Bearer " + FAKE_CALLER, FAKE_CALLER)
    assert mc.caller_authorized("bearer  " + FAKE_CALLER, FAKE_CALLER)
    assert not mc.caller_authorized("Bearer wrong", FAKE_CALLER)
    assert not mc.caller_authorized("Basic " + FAKE_CALLER, FAKE_CALLER)
    assert not mc.caller_authorized(None, FAKE_CALLER)
    assert not mc.caller_authorized("Bearer " + FAKE_CALLER, None)
    src = open("szl_router/mesh_coordinator.py", encoding="utf-8").read()
    assert "hmac.compare_digest" in src


def test_auth_reject_sends_no_upstream_credential(server, fake_upstream):
    port = server(caller_token=FAKE_CALLER)
    assert _post(port, {"Content-Type": "application/json"}) == 401
    assert _post(port, {"Authorization": "Bearer nope"}) == 401
    assert _post(port, {"Authorization": "Basic " + FAKE_CALLER}) == 401
    assert fake_upstream == []  # rejected requests never reach the upstream transport


def test_auth_status_requires_token(server, fake_upstream):
    port = server(caller_token=FAKE_CALLER)
    conn = CLIENT("127.0.0.1", port, timeout=5)
    conn.request("GET", "/coordinator/status")
    r = conn.getresponse()
    r.read()
    conn.close()
    assert r.status == 401
    assert fake_upstream == []
    conn = CLIENT("127.0.0.1", port, timeout=5)
    conn.request("GET", "/healthz")
    r = conn.getresponse()
    r.read()
    conn.close()
    assert r.status == 200
    assert fake_upstream == []


def test_auth_accept_forwards_upstream_key_not_caller_token(server, fake_upstream):
    port = server(caller_token=FAKE_CALLER)
    assert _post(port, {"Authorization": "Bearer " + FAKE_CALLER}) == 200
    assert len(fake_upstream) == 1
    assert (fake_upstream[0]["host"], fake_upstream[0]["port"]) == ("omen.example", 11434)
    assert fake_upstream[0]["path"] == "/v1/chat/completions"
    hdrs = fake_upstream[0]["headers"]
    assert hdrs.get("Authorization") == "Bearer " + FAKE_UPSTREAM
    assert FAKE_CALLER not in json.dumps(hdrs)


def test_loopback_dev_mode_without_token_keeps_existing_behaviour(server, fake_upstream):
    port = server()  # no caller token: loopback peers only
    assert _post(port, {}) == 200
    assert len(fake_upstream) == 1
    assert fake_upstream[0]["headers"].get("Authorization") == "Bearer " + FAKE_UPSTREAM


def test_proxy_can_withhold_upstream_key(fake_upstream):
    c = _coord()
    c.proxy("POST", "/chat/completions", b"{}", {}, inject_upstream_key=False)
    assert "Authorization" not in fake_upstream[0]["headers"]


AUTH = ("Authorization: Bearer " + FAKE_CALLER + "\r\n").encode()


def test_missing_content_length_is_411(server, fake_upstream):
    port = server(caller_token=FAKE_CALLER)
    req = b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n" + AUTH + b"\r\n"
    assert _raw(port, req) == 411
    req = (b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n" + AUTH
           + b"Transfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n")
    assert _raw(port, req) == 411
    assert fake_upstream == []


@pytest.mark.parametrize("value", [b"abc", b"-5", b"1e3", b"+4", b"0x10", b""])
def test_malformed_or_negative_content_length_is_400(server, fake_upstream, value):
    port = server(caller_token=FAKE_CALLER)
    req = (b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n" + AUTH
           + b"Content-Length: " + value + b"\r\n\r\n")
    assert _raw(port, req) == 400
    assert fake_upstream == []


def test_oversized_content_length_is_413(server, fake_upstream):
    port = server(caller_token=FAKE_CALLER, max_body_bytes=64)
    req = (b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n" + AUTH
           + b"Content-Length: 999999999999\r\n\r\n")
    assert _raw(port, req) == 413
    assert _post(port, {"Authorization": "Bearer " + FAKE_CALLER}, body=b"x" * 65) == 413
    assert fake_upstream == []


def test_short_body_is_400(server, fake_upstream):
    port = server(caller_token=FAKE_CALLER)
    req = (b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n" + AUTH
           + b"Content-Length: 10\r\n\r\n{}")
    assert _raw(port, req, half_close=True) == 400
    assert fake_upstream == []


@pytest.mark.parametrize("value", [b"9" * 5000, b"0" * 5000 + b"65"],
                         ids=["huge-number", "zero-padded-oversize"])
def test_long_numeric_content_length_is_413(server, fake_upstream, value):
    port = server(caller_token=FAKE_CALLER, max_body_bytes=64)
    req = (b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n" + AUTH
           + b"Content-Length: " + value + b"\r\n\r\n")
    assert _raw(port, req) == 413
    assert fake_upstream == []


@pytest.mark.parametrize("body", [b"", b"{}", b"x" * 64],
                         ids=["zero", "two-bytes", "exact-limit"])
def test_long_zero_padded_content_length_is_bounded(server, fake_upstream, body):
    port = server(caller_token=FAKE_CALLER, max_body_bytes=64)
    value = b"0" * 5000 + str(len(body)).encode("ascii")
    req = (b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n" + AUTH
           + b"Content-Length: " + value + b"\r\n\r\n" + body)
    assert _raw(port, req) == 200
    assert len(fake_upstream) == 1
    assert fake_upstream[0]["body"] == (body or None)


@pytest.mark.parametrize("values", [(b"2", b"100000"), (b"100000", b"2"),
                                  (b"2", b"2"), (b"", b"2"), (b"02", b"2")])
def test_duplicate_content_length_is_400(server, fake_upstream, values):
    port = server(caller_token=FAKE_CALLER, max_body_bytes=64)
    fields = b"".join(b"Content-Length: " + value + b"\r\n" for value in values)
    req = b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n" + AUTH + fields + b"\r\n{}"
    assert _raw(port, req) == 400
    assert fake_upstream == []


@pytest.mark.parametrize("values", [(b"",), (b"", b"chunked"),
                                  (b"chunked", b""), (b"", b"")])
def test_any_transfer_encoding_field_is_411(server, fake_upstream, values):
    port = server(caller_token=FAKE_CALLER)
    fields = b"".join(b"Transfer-Encoding: " + value + b"\r\n" for value in values)
    req = (b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n" + AUTH + fields
           + b"Content-Length: 2\r\n\r\n{}")
    assert _raw(port, req) == 411
    assert fake_upstream == []


@pytest.mark.parametrize("value", [b"\xb2", b"2, 2", b"9" * 5000 + b"x",
                                  b"\xa02\xa0", b"\x0b2\x0b"],
                         ids=["non-ascii-digit", "comma-list", "long-malformed",
                              "non-breaking-space", "vertical-tab"])
def test_content_length_requires_single_ascii_decimal(server, fake_upstream, value):
    port = server(caller_token=FAKE_CALLER, max_body_bytes=64)
    req = (b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n" + AUTH
           + b"Content-Length: " + value + b"\r\n\r\n{}")
    assert _raw(port, req) == 400
    assert fake_upstream == []


def test_content_length_allows_http_optional_whitespace(server, fake_upstream):
    port = server(caller_token=FAKE_CALLER, max_body_bytes=64)
    req = (b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n" + AUTH
           + b"Content-Length: \t2\t \r\n\r\n{}")
    assert _raw(port, req) == 200
    assert len(fake_upstream) == 1
    assert fake_upstream[0]["body"] == b"{}"


def test_auth_rejection_precedes_framing_validation(server, fake_upstream):
    port = server(caller_token=FAKE_CALLER, max_body_bytes=64)
    req = (b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
           b"Transfer-Encoding:\r\nTransfer-Encoding: chunked\r\n"
           b"Content-Length: " + b"9" * 5000 + b"\r\nContent-Length: 2\r\n\r\n{}")
    assert _raw(port, req) == 401
    assert fake_upstream == []


def test_slow_body_read_times_out_408(server, fake_upstream):
    port = server(caller_token=FAKE_CALLER, read_timeout=0.5)
    req = (b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n" + AUTH
           + b"Content-Length: 10\r\n\r\n{}")
    assert _raw(port, req, timeout=5.0) == 408
    assert fake_upstream == []


def test_rejected_request_never_logs_or_echoes_secret(server, fake_upstream, capsys):
    port = server(caller_token=FAKE_CALLER)
    conn = CLIENT("127.0.0.1", port, timeout=5)
    conn.request("POST", "/v1/chat/completions", body=b"{}",
                 headers={"Authorization": "Bearer wrong"})
    r = conn.getresponse()
    body = r.read().decode()
    conn.close()
    assert r.status == 401
    assert fake_upstream == []
    assert FAKE_CALLER not in body and FAKE_UPSTREAM not in body
    out = capsys.readouterr()
    assert FAKE_CALLER not in out.out + out.err
    assert FAKE_UPSTREAM not in out.out + out.err
    assert os.environ.get("SZL_MESH_OMEN_TOKEN") == FAKE_UPSTREAM
