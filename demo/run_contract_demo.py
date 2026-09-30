# SPDX-License-Identifier: Apache-2.0
"""One-command HTTP acceptance demo with explicit synthetic evidence."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

CALLER = "offline-demo-caller"


def require(condition, detail):
    if not condition:
        raise RuntimeError(f"Acceptance failed: {detail}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--serve", action="store_true", help="Serve fixture UI on loopback")
    parser.add_argument("--port", type=int, default=7861)
    args = parser.parse_args()
    if not 1 <= args.requests <= 2000:
        parser.error("requests must be between 1 and 2000")
    root = Path(__file__).resolve().parents[1]
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    env = os.environ.copy()
    # Do not pass existing operator/provider credentials into the demo child.
    for name in list(env):
        if any(part in name.upper() for part in ("TOKEN", "SECRET", "API_KEY", "PASSWORD", "CREDENTIAL")):
            env.pop(name)
    env["SOURCE_REVISION"] = revision
    env["PYTHONPATH"] = str(root)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = args.port if args.serve else probe.getsockname()[1]
    command = [sys.executable, "-m", "uvicorn", "demo.fixture_app:app", "--host", "127.0.0.1",
               "--port", str(port), "--no-access-log"]
    if args.serve:
        print(f"Synthetic fixture UI: http://127.0.0.1:{port}; caller field: {CALLER}", flush=True)
        return subprocess.call(command, cwd=root, env=env)
    process = subprocess.Popen(command, cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    started = time.monotonic()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(path, body=None, authorization=CALLER):
        headers = {"Accept": "application/json", "Authorization": f"Bearer {authorization}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), headers=headers)
        try:
            response = opener.open(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read()
            return response.status, raw, response.headers

    try:
        deadline = time.monotonic() + 20
        while True:
            if process.poll() is not None:
                raise RuntimeError("Fixture server failed to start")
            try:
                if call("/healthz")[0] == 200:
                    break
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("Fixture startup deadline exceeded")
            time.sleep(0.05)
        checks = {}
        for path in ("/healthz", "/readyz", "/readyz/inference", "/version", "/api/source", "/v1/models"):
            status, raw, headers = call(path)
            require(status == 200 and headers.get("Cache-Control") == "no-store", path)
            json.loads(raw)
            checks[path] = "PASS"
        for path in ("/", "/static/app.js", "/static/styles.css"):
            require(call(path)[0] == 200, path)
            checks[path] = "PASS"
        request = {"model": "szl-default", "messages": [{"role": "user", "content": "Exercise synthetic contract."}], "max_cost_tier": 0}
        require(call("/v1/chat/completions", request, authorization="invalid-fixture")[0] == 401, "caller authentication")
        require(call("/v1/chat/completions", {**request, "data_classification": "restricted"})[0] == 503, "classification policy")
        timings = []
        last_evidence = None
        for _ in range(args.requests):
            began = time.perf_counter()
            status, raw, headers = call("/v1/chat/completions", request)
            timings.append((time.perf_counter() - began) * 1000)
            require(status == 200, "completion HTTP status")
            completion = json.loads(raw)
            receipt = completion["szl_receipt"]
            require(headers["X-SZL-Receipt"] == receipt["digest"], "receipt header")
            require([attempt["state"] for attempt in receipt["attempts"]] == ["TRANSPORT_OR_CONTRACT_ERROR", "SUCCESS"], "ordered failover")
            evidence = {"completion": completion, "request": request}
            verified_status, verified, _ = call("/api/verify", evidence)
            require(verified_status == 200 and json.loads(verified)["status"] == "CONSISTENT", "receipt verification")
            last_evidence = evidence
        checks.update({"caller_auth_refusal": "PASS", "classification_refusal": "PASS", "ordered_failover": "PASS", "receipt_verification": "PASS"})
        tampered = json.loads(json.dumps(last_evidence))
        tampered["completion"]["choices"][0]["message"]["content"] = "Changed answer"
        require(call("/api/verify", tampered)[0] == 422, "tamper refusal")
        checks["tamper_refusal"] = "PASS"
        # Verify the same captured response in another process without a server.
        verifier = subprocess.run([sys.executable, "-m", "router_control.verification", "-"], input=json.dumps(last_evidence),
                                  text=True, capture_output=True, cwd=root, env=env, timeout=10)
        require(verifier.returncode == 0, "offline verifier rejected fixture evidence")
        checks["offline_verifier"] = "PASS"
        timings.sort()
        report = {"schema": "szl.router-demo/v1", "status": "PASS", "evidence_kind": "SYNTHETIC_HTTP_CONTRACT",
                  "run_id": str(uuid.uuid4()), "observed_at": datetime.now(timezone.utc).isoformat(),
                  "model_inference": "NOT_MEASURED", "trust": "UNSIGNED_HONEST", "source_revision": revision,
                  "candidate_bytes_sha256": {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in sorted((root / "router_control").rglob("*")) if path.is_file() and "__pycache__" not in path.parts},
                  "checks": checks, "requests": args.requests, "receipt_count": len(timings),
                  "fixture_http_latency_ms": {"p50": round(timings[(len(timings)-1)//2], 3), "p95": round(timings[(95*len(timings)+99)//100-1], 3)},
                  "elapsed_seconds": round(time.monotonic() - started, 3)}
        encoded = json.dumps(report, sort_keys=True, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded, encoding="utf-8")
        print(encoded, end="")
        return 0
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
