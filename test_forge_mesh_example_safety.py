# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 SZL Holdings
"""Real loopback HTTP tests with synthetic workers; no Ollama/models/provider I/O."""
from contextlib import contextmanager
from email.message import Message
import http.client
import io
from pathlib import Path
import runpy
import socket
import threading
import time
from types import SimpleNamespace

import pytest

CLIENT = http.client.HTTPConnection
PATH = Path(__file__).parent / "examples" / "forge-mesh-router.py"


def load(monkeypatch):
    monkeypatch.delenv("FORGE_GPU_HOST", raising=False)
    return SimpleNamespace(**runpy.run_path(str(PATH), run_name="mesh_test"))


class FakeSocket:
    def settimeout(self, value):
        self.timeout = value

    def shutdown(self, how):
        self.closed = True


class Response:
    def __init__(self, body=b'{"fixture":true}', status=200, headers=None, failure=None):
        self.body = io.BytesIO(body)
        self.status = status
        self.failure = failure
        self.reads = 0
        self.closed = False
        self.headers = Message()
        for k, v in (headers if headers is not None else [("Content-Type", "application/json")]):
            self.headers[k] = v

    def read(self, size):
        self.reads += 1
        if self.failure and self.reads > 1:
            raise self.failure
        return self.body.read(size)

    read1 = read

    def close(self):
        self.closed = True
        self.body.close()

    def getheaders(self):
        return list(self.headers.items())


class Connection:
    def __init__(self, *, response=None, phase=None):
        self.sock = FakeSocket()
        self.response = response or Response()
        self.phase = phase
        self.requests = []
        self.closed = False

    def connect(self):
        if self.phase == "connect":
            raise ConnectionRefusedError("private upstream information")

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path, body, headers))
        if self.phase == "send":
            raise BrokenPipeError("private upstream information")

    def getresponse(self):
        if self.phase == "headers":
            raise http.client.RemoteDisconnected("private upstream information")
        return self.response

    def close(self):
        self.closed = True


@contextmanager
def serving(module, monkeypatch, connections=None):
    pending = list(connections or [Connection()])
    used = []

    def factory(host, port, timeout):
        if not pending:
            raise AssertionError("unexpected worker invocation")
        result = pending.pop(0)
        used.append((host, port, result))
        return result

    monkeypatch.setattr(http.client, "HTTPConnection", factory)
    with module.LocalMeshServer(("127.0.0.1", 0)) as server:
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        worker.start()
        try:
            yield server, used
        finally:
            server.shutdown()
            worker.join(timeout=2)
            assert not worker.is_alive()
            # Await the real request handlers' finite cleanup, not an external job.
            ends = time.monotonic() + 2
            while any(module.inflight.values()) and time.monotonic() < ends:
                time.sleep(0.005)
            assert all(n == 0 for n in module.inflight.values())
            assert all(c.closed for _, _, c in used)


def request(server, *, method="POST", path="/api/chat", body=b'{"model":"synthetic-fixture"}', headers=None):
    connection = CLIENT(*server.server_address, timeout=2)
    try:
        connection.request(method, path, body=body, headers=headers or {"Content-Type": "application/json"})
        response = connection.getresponse()
        data = response.read()
        return response.status, data, response.getheaders()
    finally:
        connection.close()


def raw_request(server, payload):
    host = f"127.0.0.1:{server.server_port}".encode()
    payload = payload.replace(b"HOST", host)
    with socket.create_connection(server.server_address, timeout=2) as sock:
        sock.sendall(payload)
        sock.shutdown(socket.SHUT_WR)
        result = bytearray()
        while True:
            try:
                block = sock.recv(8192)
            except ConnectionResetError:
                break
            if not block:
                return bytes(result)
            result.extend(block)
        return bytes(result)


@pytest.mark.parametrize("host", ["localhost", "0.0.0.0", "10.0.0.1", "100.64.0.2", "8.8.8.8", "::", "::1%lo", "127.0.0.1 "])
def test_remote_or_ambiguous_backend_host_rejected(monkeypatch, host):
    module = load(monkeypatch)
    with pytest.raises(ValueError):
        module.loopback_host(host)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1"])
def test_literal_loopback_backend_allowed(monkeypatch, host):
    assert load(monkeypatch).loopback_host(host) == host


def test_listener_refuses_nonloopback_before_binding(monkeypatch):
    module = load(monkeypatch)
    with pytest.raises(ValueError):
        module.LocalMeshServer(("0.0.0.0", 0))
    assert module.LISTEN == ("127.0.0.1", 11500)


@pytest.mark.parametrize("phase", ["send", "headers"])
def test_dispatch_or_response_failure_never_replays(monkeypatch, phase):
    module = load(monkeypatch)
    first, spare = Connection(phase=phase), Connection()
    with serving(module, monkeypatch, [first, spare]) as (server, used):
        status, data, _ = request(server)
        assert status == 502
        assert len(used) == 1
        assert len(first.requests) == 1
        assert spare.requests == []
        assert b"OUTCOME_UNKNOWN" in data
        assert b"private upstream information" not in data


def test_pre_dispatch_connect_failure_can_try_one_fallback(monkeypatch):
    module = load(monkeypatch)
    first, second = Connection(phase="connect"), Connection()
    with serving(module, monkeypatch, [first, second]) as (server, used):
        status, data, headers = request(server)
        assert status == 200 and data == b'{"fixture":true}'
        assert len(used) == 2 and first.requests == [] and len(second.requests) == 1
        assert "x-forge-backend" not in dict((k.lower(), v) for k, v in headers)
        forwarded = second.requests[0][3]
        assert set(forwarded) == {"Content-Type", "Connection"}


def test_both_connect_failures_are_redacted_and_finite(monkeypatch):
    module = load(monkeypatch)
    with serving(module, monkeypatch, [Connection(phase="connect"), Connection(phase="connect")]) as (server, used):
        status, data, _ = request(server)
        assert status == 502 and len(used) == 2
        assert b"dgpu:CONNECT_FAILED" in data and b"igpu:CONNECT_FAILED" in data
        assert b"private upstream" not in data


@pytest.mark.parametrize("path", ["/api/pull", "/api/create", "/api/copy", "/api/delete", "/api/chat?x=1", "/api/%63hat", "//api/chat", "http://other.invalid/api/chat", "/x/../api/chat"])
def test_arbitrary_or_noncanonical_routes_do_not_reach_workers(monkeypatch, path):
    module = load(monkeypatch)
    with serving(module, monkeypatch) as (server, used):
        status, _, _ = request(server, path=path, headers={
            "Content-Type": "application/json", "Host": f"127.0.0.1:{server.server_port}",
        })
        assert status in {400, 404}
        assert used == []


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH", "OPTIONS"])
def test_mutation_methods_do_not_reach_workers(monkeypatch, method):
    module = load(monkeypatch)
    with serving(module, monkeypatch) as (server, used):
        assert request(server, method=method)[0] == 405
        assert used == []


@pytest.mark.parametrize("headers", [
    {"Origin": "https://foreign.invalid"}, {"Sec-Fetch-Site": "cross-site"},
    {"Cookie": "private-fixture"}, {"Authorization": "private-fixture"},
    {"Host": "foreign.invalid"}, {"Proxy-Authorization": "private-fixture"},
    {"Upgrade": "websocket"},
])
def test_browser_credentials_and_foreign_hosts_are_rejected(monkeypatch, headers):
    module = load(monkeypatch)
    with serving(module, monkeypatch) as (server, used):
        status, data, _ = request(server, headers={"Content-Type": "application/json", **headers})
        assert status == 403 and used == []
        assert b"private-fixture" not in data


@pytest.mark.parametrize("extra,expected", [
    (b"Content-Length: 2\r\nContent-Length: 2\r\n", 400),
    (b"Content-Length: +2\r\n", 400), (b"Content-Length: -1\r\n", 400),
    (b"Content-Length: 1048577\r\n", 413),
    (b"Transfer-Encoding: chunked\r\nContent-Length: 2\r\n", 400),
    (b"Content-Length: 2\r\nContent-Encoding: gzip\r\n", 415),
    (b"Content-Length: 2\r\nHost: HOST\r\n", 400),
    (b"Content-Length: 2\r\nExpect: 100-continue\r\n", 417),
    (b"", 411),
])
def test_wire_framing_failures(monkeypatch, extra, expected):
    module = load(monkeypatch)
    with serving(module, monkeypatch) as (server, used):
        data = raw_request(server, b"POST /api/chat HTTP/1.1\r\nHost: HOST\r\nContent-Type: application/json\r\n" + extra + b"\r\n{}")
        assert f"HTTP/1.1 {expected} ".encode() in data
        assert used == []


@pytest.mark.parametrize("body", [
    b"[]", b"{}", b'{"model":"x","model":"y"}',
    b'{"model":"x","stream":1}', b'{"model":"x","temperature":NaN}',
    b'{"model":"x","temperature":1e999}', b'{"model":"x","prompt":"\\ud800"}',
    b'{"model":"x","options":{"seed":' + b"9" * 129 + b'}}',
    b'{"model":"x","prompt":' + b"[" * 70 + b"0" + b"]" * 70 + b'}',
])
def test_json_boundaries(monkeypatch, body):
    module = load(monkeypatch)
    with serving(module, monkeypatch) as (server, used):
        assert request(server, body=body)[0] == 422
        assert used == []


@pytest.mark.parametrize("path", ["/api/chat", "/api/generate", "/v1/chat/completions", "/v1/completions", "/api/show", "/api/embed", "/api/embeddings", "/v1/embeddings"])
def test_supported_inference_paths_remain_available(monkeypatch, path):
    module = load(monkeypatch)
    with serving(module, monkeypatch) as (server, used):
        status, _, _ = request(server, path=path)
        assert status == 200 and len(used) == 1
        assert used[0][1] == (11435 if path in module.EMBED_PATHS else 11434)


def test_status_is_observation_only_not_liveness_probe(monkeypatch):
    module = load(monkeypatch)
    with serving(module, monkeypatch) as (server, used):
        status, data, _ = request(server, method="GET", path="/mesh/status", body=None)
        assert status == 200 and b"NOT_PROBED" in data and used == []
        assert b"11434" not in data and b"127.0.0.1" not in data


@pytest.mark.parametrize("headers", [
    [("Content-Type", "application/json"), ("Content-Length", "-1")],
    [("Content-Type", "application/json"), ("Content-Length", "2"), ("Content-Length", "2")],
    [("Content-Type", "application/json"), ("Transfer-Encoding", "chunked"), ("Content-Length", "2")],
    [("Content-Type", "application/json"), ("Content-Encoding", "gzip")],
    [("Content-Type", "text/html")],
    [("Content-Type", "application/json"), ("Transfer-Encoding", "")],
    [("Content-Type", "application/json;\r\nX-Evil: yes")],
    [("Content-Type", "application/json"), ("Content-Type", "application/json")],
])
def test_upstream_header_failures_are_not_replayed(monkeypatch, headers):
    module = load(monkeypatch)
    with serving(module, monkeypatch, [Connection(response=Response(headers=headers)), Connection()]) as (server, used):
        assert request(server)[0] == 502
        assert len(used) == 1


@pytest.mark.parametrize("status", [301, 400, 429, 500, 503])
def test_upstream_status_is_not_retry_authority(monkeypatch, status):
    module = load(monkeypatch)
    response = Response(body=b"private backend diagnostics", status=status)
    with serving(module, monkeypatch, [Connection(response=response), Connection()]) as (server, used):
        result, data, _ = request(server)
        assert result == (502 if status == 301 else status)
        assert b"private backend" not in data and len(used) == 1


@pytest.mark.parametrize("kind", ["overflow", "short", "timeout"])
def test_partial_stream_cannot_be_a_complete_http_response(monkeypatch, kind):
    module = load(monkeypatch)
    if kind == "overflow":
        module.H._relay.__globals__["MAX_RESPONSE_BYTES"] = 4
        response = Response(body=b"12345")
    elif kind == "short":
        response = Response(body=b"123", headers=[("Content-Type", "application/json"), ("Content-Length", "4")])
    else:
        response = Response(body=b"123", failure=TimeoutError("private backend details"))
    with serving(module, monkeypatch, [Connection(response=response), Connection()]) as (server, used):
        with pytest.raises(http.client.IncompleteRead):
            request(server)
        assert len(used) == 1


def test_header_budget_rejects_before_unbounded_allocation(monkeypatch):
    module = load(monkeypatch)
    stream = io.BytesIO(b"a" * (module.MAX_HEADER_BYTES + 2))
    with pytest.raises(module.Rejected):
        module.HeaderBudget(stream).readline()
    assert stream.tell() == module.MAX_HEADER_BYTES + 1


def test_upstream_header_parser_uses_same_raw_budget(monkeypatch):
    module = load(monkeypatch)
    data = b"HTTP/1.1 200 OK\r\nX: " + b"x" * module.MAX_HEADER_BYTES + b"\r\n\r\n"
    response = module.BoundedResponse(SimpleNamespace(makefile=lambda *args: io.BytesIO(data)))
    with pytest.raises(module.Rejected):
        response.begin()
    response.close()


def test_request_headers_have_raw_byte_bound(monkeypatch):
    module = load(monkeypatch)
    with serving(module, monkeypatch) as (server, used):
        data = raw_request(server, b"GET /api/tags HTTP/1.1\r\nHost: HOST\r\nX: " + b"x" * module.MAX_HEADER_BYTES + b"\r\n\r\n")
        assert b"HTTP/1.1 431 " in data and used == []


def test_body_deadline_ends_incomplete_request(monkeypatch):
    module = load(monkeypatch)
    module.H._request_body.__globals__["BODY_SECONDS"] = 0.1
    with serving(module, monkeypatch) as (server, used):
        with socket.create_connection(server.server_address, timeout=2) as sock:
            prefix = f"POST /api/chat HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{{".encode()
            sock.sendall(prefix)
            assert sock.recv(1) == b""
        assert used == []


def test_header_deadline_ends_slow_incomplete_header(monkeypatch):
    module = load(monkeypatch)
    module.H.handle_one_request.__globals__["HEADER_SECONDS"] = 0.1
    with serving(module, monkeypatch) as (server, used):
        with socket.create_connection(server.server_address, timeout=2) as sock:
            sock.sendall(b"GET /mesh/status HTTP/1.1\r\nHost:")
            assert sock.recv(1) == b""
        assert used == []


def test_server_admission_is_finite_without_starting_more_threads(monkeypatch):
    module = load(monkeypatch)
    with module.LocalMeshServer(("127.0.0.1", 0)) as server:
        for _ in range(module.MAX_CLIENTS):
            assert server._slots.acquire(blocking=False)
        closed = []
        monkeypatch.setattr(server, "shutdown_request", lambda sock: closed.append(sock))
        token = object()
        server.process_request(token, ("127.0.0.1", 1))
        assert closed == [token]
        for _ in range(module.MAX_CLIENTS):
            server._slots.release()


@pytest.mark.parametrize("method,path", [("GET", "/api/version"), ("GET", "/api/tags"), ("GET", "/v1/models"), ("HEAD", "/api/tags")])
def test_read_routes_and_head_remain_bounded(monkeypatch, method, path):
    module = load(monkeypatch)
    with serving(module, monkeypatch) as (server, used):
        status, data, _ = request(server, method=method, path=path, body=None)
        assert status == 200 and len(used) == 1
        if method == "HEAD":
            assert data == b""


def test_expired_deadline_does_not_start_an_attempt_or_leak_reservation(monkeypatch):
    module = load(monkeypatch)
    module.H._proxy.__globals__["UPSTREAM_SECONDS"] = 0
    with serving(module, monkeypatch) as (server, used):
        with pytest.raises((http.client.RemoteDisconnected, ConnectionError)):
            request(server)
        assert used == []


def test_real_worker_consumes_request_then_disconnects_without_replay(monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    module = load(monkeypatch)
    consumed = []

    class Worker(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            consumed.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)

    with ThreadingHTTPServer(("127.0.0.1", 0), Worker) as worker:
        thread = threading.Thread(target=worker.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        actual = ("127.0.0.1", worker.server_port)
        globals_ = module.H._proxy.__globals__
        globals_["DGPU"] = actual
        globals_["IGPU"] = ("127.0.0.2", worker.server_port)
        module.inflight.clear()
        module.inflight.update({globals_["DGPU"]: 0, globals_["IGPU"]: 0})
        try:
            with module.LocalMeshServer(("127.0.0.1", 0)) as proxy:
                pt = threading.Thread(target=proxy.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
                pt.start()
                try:
                    status, data, _ = request(proxy)
                    assert status == 502 and b"dgpu:OUTCOME_UNKNOWN" in data
                    assert b"igpu" not in data and len(consumed) == 1
                finally:
                    proxy.shutdown()
                    pt.join(2)
        finally:
            worker.shutdown()
            thread.join(2)
        assert all(n == 0 for n in module.inflight.values())


def test_real_upstream_wall_deadline_breaks_incomplete_stream(monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    module = load(monkeypatch)
    release = threading.Event()
    received = []

    class Worker(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "50")
            self.end_headers()
            self.wfile.write(b'{"partial":')
            self.wfile.flush()
            release.wait(2)
            self.close_connection = True

    with ThreadingHTTPServer(("127.0.0.1", 0), Worker) as worker:
        thread = threading.Thread(target=worker.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        globals_ = module.H._proxy.__globals__
        globals_["UPSTREAM_SECONDS"] = 0.2
        globals_["DGPU"] = ("127.0.0.1", worker.server_port)
        globals_["IGPU"] = ("127.0.0.2", worker.server_port)
        module.inflight.clear()
        module.inflight.update({globals_["DGPU"]: 0, globals_["IGPU"]: 0})
        try:
            with module.LocalMeshServer(("127.0.0.1", 0)) as proxy:
                pt = threading.Thread(target=proxy.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
                pt.start()
                try:
                    with pytest.raises(http.client.IncompleteRead):
                        request(proxy)
                    assert len(received) == 1
                finally:
                    release.set()
                    proxy.shutdown()
                    pt.join(2)
        finally:
            release.set()
            worker.shutdown()
            thread.join(2)
        assert all(n == 0 for n in module.inflight.values())


@pytest.mark.parametrize("status", [204, 205, 206])
def test_empty_or_partial_success_status_is_not_full_inference(monkeypatch, status):
    module = load(monkeypatch)
    with serving(module, monkeypatch, [Connection(response=Response(status=status))]) as (server, used):
        assert request(server)[0] == 502
        assert len(used) == 1 and used[0][2].response.closed


def test_response_and_reservation_are_released_when_close_raises(monkeypatch):
    module = load(monkeypatch)
    connection = Connection(response=Response(status=503))
    def failed_close():
        connection.closed = True
        raise OSError("private close diagnostic")
    connection.close = failed_close
    with serving(module, monkeypatch, [connection]) as (server, used):
        status, data, _ = request(server)
        assert status == 503
        assert b"private close" not in data
        assert connection.response.closed
        assert all(n == 0 for n in module.inflight.values())


def test_real_chunked_stream_delivers_before_worker_finishes(monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    module = load(monkeypatch)
    release = threading.Event()

    class Worker(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *args):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            self.wfile.write(b'5\r\nhello\r\n')
            self.wfile.flush()
            release.wait(2)
            self.wfile.write(b'0\r\n\r\n')
            self.wfile.flush()
            self.close_connection = True

    with ThreadingHTTPServer(('127.0.0.1', 0), Worker) as worker:
        wt = threading.Thread(target=worker.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        wt.start()
        globals_ = module.H._proxy.__globals__
        globals_['DGPU'] = ('127.0.0.1', worker.server_port)
        globals_['IGPU'] = ('127.0.0.2', worker.server_port)
        module.inflight.clear()
        module.inflight.update({globals_['DGPU']: 0, globals_['IGPU']: 0})
        try:
            with module.LocalMeshServer(('127.0.0.1', 0)) as proxy:
                pt = threading.Thread(target=proxy.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
                pt.start()
                client = CLIENT(*proxy.server_address, timeout=1)
                try:
                    client.request('POST', '/api/chat', body=b'{"model":"synthetic-fixture"}',
                                   headers={'Content-Type': 'application/json'})
                    response = client.getresponse()
                    assert response.status == 200
                    assert response.read1(5) == b'hello'
                    assert not release.is_set()
                    release.set()
                    assert response.read() == b''
                finally:
                    release.set()
                    client.close()
                    proxy.shutdown()
                    pt.join(2)
                    assert not pt.is_alive()
        finally:
            release.set()
            worker.shutdown()
            wt.join(2)
            assert not wt.is_alive()
        assert all(n == 0 for n in module.inflight.values())
