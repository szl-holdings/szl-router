#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 SZL Holdings
"""Bounded loopback-only inference example; not a production/network gateway.

Importing this file starts nothing. Local clients share the OS trust boundary:
there is no user authentication, model authorization, or execution receipt here.
See docs/DUAL_GPU_MESH.md before running. No request is replayed after dispatch.
"""
from __future__ import annotations

from contextlib import contextmanager
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
import os
import re
import socket
import threading
import time
from typing import Any, Iterator


def loopback_host(value: str) -> str:
    """No DNS resolution or remote-host fallback in this local-only example."""
    if "%" in value:
        raise ValueError("zone identifiers are not supported")
    address = ipaddress.ip_address(value)
    if not address.is_loopback:
        raise ValueError("the mesh example requires a literal loopback address")
    return str(address)


GPU = loopback_host(os.environ.get("FORGE_GPU_HOST", "127.0.0.1"))
DGPU = (GPU, 11434)
IGPU = (GPU, 11435)
LISTEN = ("127.0.0.1", 11500)
EMBED_PATHS = ("/api/embeddings", "/api/embed", "/v1/embeddings")
GET_PATHS = frozenset({"/api/version", "/api/tags", "/v1/models"})
POST_PATHS = frozenset({*EMBED_PATHS, "/api/chat", "/api/generate",
                        "/v1/chat/completions", "/v1/completions", "/api/show"})
MAX_HEADER_BYTES = 32_768
MAX_BODY_BYTES = 1_048_576
MAX_RESPONSE_BYTES = 8_388_608
MAX_CLIENTS = 8
HEADER_SECONDS = 5.0
BODY_SECONDS = 5.0
UPSTREAM_SECONDS = 30.0
CONNECT_SECONDS = 3.0
MEDIA_TYPES = frozenset({"application/json", "application/x-ndjson", "text/event-stream"})
lock = threading.RLock()
inflight = {DGPU: 0, IGPU: 0}


class Rejected(ValueError):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


class HeaderBudget:
    """Bound raw header bytes before the stdlib parser allocates its header list."""
    def __init__(self, stream: Any):
        self.stream = stream
        self.remaining: int | None = MAX_HEADER_BYTES

    def readline(self, limit: int = -1) -> bytes:
        if self.remaining is None:
            return self.stream.readline(limit)
        size = self.remaining + 1
        if limit >= 0:
            size = min(size, limit)
        data = self.stream.readline(size)
        self.remaining -= len(data)
        if self.remaining < 0:
            raise Rejected(431, "HEADER_LIMIT")
        return data

    def __getattr__(self, name: str) -> Any:
        return getattr(self.stream, name)


class BoundedResponse(http.client.HTTPResponse):
    def begin(self) -> None:
        wrapped = HeaderBudget(self.fp)
        self.fp = wrapped
        super().begin()
        wrapped.remaining = None


def _shutdown(sock: Any) -> None:
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class Deadline:
    """Wall-clock deadline, including slow-drip reads; not just an idle timeout."""
    def __init__(self, seconds: float, sockets: Any):
        self.sockets = sockets
        self.ends_at = time.monotonic() + seconds
        self.expired = threading.Event()
        self.timer = threading.Timer(seconds, self._expire)
        self.timer.daemon = True
        self.timer.start()

    def _expire(self) -> None:
        self.expired.set()
        for sock in self.sockets():
            _shutdown(sock)

    def remaining(self) -> float:
        left = self.ends_at - time.monotonic()
        if left <= 0 or self.expired.is_set():
            raise TimeoutError("request phase deadline")
        return left

    def finish(self) -> None:
        self.timer.cancel()
        self.timer.join()
        if self.expired.is_set():
            raise TimeoutError("request phase deadline")


@contextmanager
def deadline(seconds: float, sockets: Any) -> Iterator[Deadline]:
    guard = Deadline(seconds, sockets)
    try:
        yield guard
    finally:
        guard.finish()


def pick_backend(path: str) -> tuple[tuple[str, int], tuple[str, int]]:
    p = path.split("?", 1)[0]
    if p in EMBED_PATHS:
        return IGPU, DGPU
    with lock:
        return (DGPU, IGPU) if inflight[DGPU] <= inflight[IGPU] else (IGPU, DGPU)


def _one(headers: Any, name: str) -> str | None:
    values = headers.get_all(name, [])
    if len(values) > 1:
        raise Rejected(400, "DUPLICATE_HEADER")
    return values[0] if values else None


def content_length(headers: Any, *, maximum: int, required: bool) -> int:
    if headers.get_all("Transfer-Encoding", []):
        raise Rejected(400, "TRANSFER_ENCODING_UNSUPPORTED")
    if headers.get_all("Content-Encoding", []):
        raise Rejected(415, "CONTENT_ENCODING_UNSUPPORTED")
    raw = _one(headers, "Content-Length")
    if raw is None:
        if required:
            raise Rejected(411, "CONTENT_LENGTH_REQUIRED")
        return 0
    if not re.fullmatch(r"[0-9]{1,10}", raw):
        raise Rejected(400, "INVALID_CONTENT_LENGTH")
    length = int(raw)
    if length > maximum:
        raise Rejected(413, "BODY_LIMIT")
    return length


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _nonfinite(value: str) -> None:
    raise ValueError("non-finite JSON")


def _number(value: str) -> int | float:
    if len(value) > 128:
        raise ValueError("JSON numeric token limit")
    result = float(value) if any(c in value for c in ".eE") else int(value)
    if isinstance(result, float) and not math.isfinite(result):
        raise ValueError("non-finite JSON")
    return result


def _validate_tree(value: Any, depth: int = 0) -> None:
    if depth > 64:
        raise ValueError("JSON nesting limit")
    if isinstance(value, str):
        value.encode("utf-8")
    elif isinstance(value, dict):
        for key, child in value.items():
            key.encode("utf-8")
            _validate_tree(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _validate_tree(child, depth + 1)


def validate_body(raw: bytes) -> None:
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                          parse_constant=_nonfinite, parse_float=_number, parse_int=_number)
        _validate_tree(data)
        if not isinstance(data, dict):
            raise ValueError("JSON object required")
        model = data.get("model")
        if not isinstance(model, str) or not model.strip() or len(model) > 256:
            raise ValueError("model required")
        model.encode("utf-8")
        if any(ord(char) < 32 or ord(char) == 127 for char in model):
            raise ValueError("invalid model")
        if "stream" in data and type(data["stream"]) is not bool:
            raise ValueError("stream must be a boolean")
    except (ValueError, UnicodeError, RecursionError):
        raise Rejected(422, "INVALID_JSON_REQUEST") from None


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    rbufsize = 0

    def log_message(self, *args: Any) -> None:
        # No prompt, credential, path or backend exception logging.
        return

    def handle_one_request(self) -> None:
        self.headers_sent = False
        self._upstream_socket: Any = None
        original = self.rfile
        self.rfile = HeaderBudget(original)
        self._header_deadline = Deadline(HEADER_SECONDS, lambda: (self.connection,))
        try:
            super().handle_one_request()
        except Rejected as exc:
            if hasattr(self, "request_version"):
                self._send_json(exc.status, {"error": exc.code})
        except (OSError, http.client.HTTPException, TimeoutError):
            pass
        finally:
            self._header_deadline.timer.cancel()
            self._header_deadline.timer.join()
            self.rfile = original
            self.close_connection = True  # No second/pipelined request.

    def handle_expect_100(self) -> bool:
        self._send_json(417, {"error": "EXPECT_UNSUPPORTED"})
        return False

    def send_error(self, code: int, message: str | None = None,
                   explain: str | None = None) -> None:
        self._send_json(code, {"error": "HTTP_REQUEST_REJECTED"})

    def _send_json(self, code: int, obj: dict[str, Any]) -> None:
        if self.headers_sent:
            return
        body = json.dumps(obj, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.close_connection = True
        try:
            self.send_response_only(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.headers_sent = True
            self.end_headers()
            if getattr(self, "command", None) != "HEAD":
                self.wfile.write(body)
        except OSError:
            pass

    def _request_body(self) -> bytes | None:
        expected_host = f"{self.server.server_address[0]}:{self.server.server_address[1]}"
        if _one(self.headers, "Host") != expected_host:
            raise Rejected(403, "HOST_REJECTED")
        for name in ("Origin", "Sec-Fetch-Site", "Cookie", "Authorization",
                     "Proxy-Authorization", "Expect", "Upgrade"):
            if self.headers.get_all(name, []):
                raise Rejected(403, "BROWSER_OR_CREDENTIAL_INPUT_REJECTED")
        target = self.raw_requestline.split()
        if len(target) != 3 or target[1] != self.path.encode("iso-8859-1"):
            raise Rejected(400, "NONCANONICAL_REQUEST_TARGET")
        if self.request_version != "HTTP/1.1":
            raise Rejected(400, "HTTP_VERSION_UNSUPPORTED")
        if self.command in {"GET", "HEAD"}:
            allowed = GET_PATHS | {"/mesh/status"}
        elif self.command == "POST":
            allowed = POST_PATHS
        else:
            raise Rejected(405, "METHOD_REJECTED")
        if self.path not in allowed:
            raise Rejected(404, "ROUTE_REJECTED")
        length = content_length(self.headers, maximum=MAX_BODY_BYTES,
                                required=self.command == "POST")
        if self.command != "POST":
            if length:
                raise Rejected(400, "BODY_NOT_ALLOWED")
            return None
        media = _one(self.headers, "Content-Type")
        if media not in {"application/json", "application/json; charset=utf-8"}:
            raise Rejected(415, "JSON_CONTENT_TYPE_REQUIRED")
        chunks: list[bytes] = []
        remaining = length
        with deadline(BODY_SECONDS, lambda: (self.connection,)):
            while remaining:
                block = self.rfile.read(min(remaining, 65_536))
                if not block:
                    raise Rejected(400, "INCOMPLETE_BODY")
                chunks.append(block)
                remaining -= len(block)
        body = b"".join(chunks)
        validate_body(body)
        return body

    def _relay(self, response: http.client.HTTPResponse) -> None:
        if not 200 <= response.status < 300:
            code = response.status if 400 <= response.status <= 599 else 502
            raise Rejected(code, "UPSTREAM_REJECTED")
        if response.status in {204, 205, 206}:
            raise Rejected(502, "UPSTREAM_EMPTY_OR_PARTIAL_STATUS")
        headers = response.headers
        try:
            media = _one(headers, "Content-Type")
            length = _one(headers, "Content-Length")
            transfer = _one(headers, "Transfer-Encoding")
        except Rejected:
            raise Rejected(502, "UPSTREAM_DUPLICATE_HEADER") from None
        if media and (len(media) > 128 or any(ord(c) < 32 or ord(c) >= 127 for c in media)):
            raise Rejected(502, "UPSTREAM_MEDIA_REJECTED")
        if not media or media.split(";", 1)[0].strip().lower() not in MEDIA_TYPES:
            raise Rejected(502, "UPSTREAM_MEDIA_REJECTED")
        if headers.get_all("Content-Encoding", []):
            raise Rejected(502, "UPSTREAM_ENCODING_REJECTED")
        if transfer is not None and (transfer.lower() != "chunked" or length is not None):
            raise Rejected(502, "UPSTREAM_FRAMING_REJECTED")
        expected = None
        if length is not None:
            if not re.fullmatch(r"[0-9]{1,10}", length):
                raise Rejected(502, "UPSTREAM_FRAMING_REJECTED")
            expected = int(length)
            if expected > MAX_RESPONSE_BYTES:
                raise Rejected(502, "UPSTREAM_BODY_LIMIT")
        self.send_response_only(response.status)
        self.send_header("Content-Type", media)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        if self.command != "HEAD":
            self.send_header("Transfer-Encoding", "chunked")
        self.headers_sent = True
        self.end_headers()
        if self.command == "HEAD":
            return
        total = 0
        while True:
            block = response.read1(min(8192, MAX_RESPONSE_BYTES - total + 1))
            if not block:
                if expected is not None and total != expected:
                    raise Rejected(502, "UPSTREAM_INCOMPLETE_BODY")
                self.wfile.write(b"0\r\n\r\n")
                return
            total += len(block)
            if total > MAX_RESPONSE_BYTES:
                raise Rejected(502, "UPSTREAM_BODY_LIMIT")
            self.wfile.write(f"{len(block):x}\r\n".encode("ascii") + block + b"\r\n")
            self.wfile.flush()

    def _proxy(self) -> None:
        self._header_deadline.finish()
        self.rfile.remaining = None
        attempts: list[str] = []
        try:
            body = self._request_body()
            if self.path == "/mesh/status":
                with lock:
                    counts = {"dgpu": inflight[DGPU], "igpu": inflight[IGPU]}
                self._send_json(200, {"mesh": "forge-dual-gpu", "scope": "LOOPBACK_REFERENCE",
                                     "inflight": counts, "worker_health": "NOT_PROBED",
                                     "execution_authority": "NONE"})
                return
            with deadline(UPSTREAM_SECONDS, lambda: (self.connection, self._upstream_socket)) as guard:
                for index in range(2):
                    guard.remaining()
                    with lock:
                        if index == 0:
                            backend, fallback = pick_backend(self.path)
                        else:
                            backend = fallback
                        inflight[backend] += 1
                    conn = None
                    response = None
                    label = "dgpu" if backend == DGPU else "igpu"
                    dispatched = False
                    try:
                        conn = http.client.HTTPConnection(*backend, timeout=CONNECT_SECONDS)
                        conn.response_class = BoundedResponse
                        # Only a connect failure is known to precede all request bytes.
                        guard.remaining()
                        conn.connect()
                        self._upstream_socket = conn.sock
                        conn.sock.settimeout(guard.remaining())
                        dispatched = True
                        conn.request(self.command, self.path, body=body,
                                     headers={"Content-Type": "application/json",
                                              "Connection": "close"})
                        try:
                            response = conn.getresponse()
                        except Rejected:
                            raise Rejected(502, "UPSTREAM_HEADER_LIMIT") from None
                        self._relay(response)
                        return
                    except (OSError, http.client.HTTPException, Rejected, TimeoutError) as exc:
                        attempts.append(f"{label}:" + ("OUTCOME_UNKNOWN" if dispatched else "CONNECT_FAILED"))
                        if not dispatched and index == 0:
                            continue
                        if isinstance(exc, Rejected):
                            self._send_json(exc.status, {"error": exc.code, "attempts": attempts})
                        else:
                            self._send_json(502, {"error": "UPSTREAM_UNAVAILABLE_OR_INCOMPLETE", "attempts": attempts})
                        return
                    finally:
                        self._upstream_socket = None
                        try:
                            if response is not None:
                                response.close()
                        finally:
                            try:
                                if conn is not None:
                                    conn.close()
                            finally:
                                with lock:
                                    inflight[backend] -= 1
        except Rejected as exc:
            self._send_json(exc.status, {"error": exc.code})
        except (OSError, http.client.HTTPException, TimeoutError):
            self._send_json(408, {"error": "REQUEST_INCOMPLETE"})
        finally:
            self.close_connection = True

    do_GET = _proxy
    do_HEAD = _proxy
    do_POST = _proxy
    do_PUT = _proxy
    do_DELETE = _proxy
    do_PATCH = _proxy
    do_OPTIONS = _proxy


class LocalMeshServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], handler: Any = H):
        if address[0] != "127.0.0.1":
            raise ValueError("the mesh listener must bind IPv4 loopback")
        self._slots = threading.BoundedSemaphore(MAX_CLIENTS)
        super().__init__(address, handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # No traceback with caller/provider values; the request is closed.
        return


if __name__ == "__main__":
    with LocalMeshServer(LISTEN) as srv:
        print("forge-mesh-router: loopback reference on 127.0.0.1:11500", flush=True)
        srv.serve_forever()
