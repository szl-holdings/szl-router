"""Real loopback inference through the router ASGI API, with offline receipts."""
from __future__ import annotations

import argparse
import base64
import copy
from contextlib import ExitStack
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DEFAULT_BACKEND = "http://127.0.0.1:11434"


def loopback_root(value):
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment
            or parsed.port is None or not 1 <= parsed.port <= 65535):
        raise ValueError("backend must be a fixed IPv4 loopback root")
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("loopback backend redirect refused")


def local_json(path, backend_root=DEFAULT_BACKEND):
    # Ignore proxy settings and forbid redirects outside the local backend.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(loopback_root(backend_root) + path, timeout=10) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError("backend metadata too large")
    return json.loads(raw)


def model_identity(model, backend_root=DEFAULT_BACKEND):
    matches = [m for m in local_json("/api/tags", backend_root)["models"] if m["name"] == model]
    if len(matches) != 1:
        raise ValueError("requested model is not installed; demo never downloads weights")
    result = matches[0]
    digest = result.get("digest", "")
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("model manifest digest unavailable")
    return {"name": model, "manifest_sha256": digest, "details": result.get("details")}


def percentile(values, quantile):
    return sorted(values)[max(0, math.ceil(len(values) * quantile) - 1)]


def check_response(response, model, messages, public_key, backend_root=DEFAULT_BACKEND):
    from szl_router import receipts
    response.raise_for_status()
    body = response.json()
    provenance = body["x_szl_provenance"]
    expected = "local_ollama:" + model
    if provenance.get("served_by") != expected or body.get("model") != model:
        raise ValueError("response model/provider mismatch")
    if provenance.get("base_url") != loopback_root(backend_root) + "/v1":
        raise ValueError("response escaped loopback route")
    content = body["choices"][0]["message"].get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("empty model output")
    envelope = receipts.decode_header(response.headers["x-szl-receipt"])
    valid, detail = receipts.verify_envelope(envelope, public_key)
    if not valid:
        raise ValueError("receipt verification failed: " + detail)
    payload = json.loads(base64.b64decode(envelope["payload"]))
    if payload.get("request_digest") != receipts.request_digest(expected, messages):
        raise ValueError("receipt request binding mismatch")
    if payload.get("served_by") != expected:
        raise ValueError("receipt provider binding mismatch")
    tampered = copy.deepcopy(envelope)
    tampered["payload"] = envelope["payload"][:-4] + "AAAA"
    if receipts.verify_envelope(tampered, public_key)[0]:
        raise ValueError("tampered receipt accepted")
    return body, envelope


def run(model, count, output, deadline, max_tokens=48, isolated_cpu=False):
    output.mkdir(parents=True, exist_ok=False)
    report = {"schema": "szl.router-local-demo/v1", "status": "FAIL",
              "started_at": datetime.now(timezone.utc).isoformat(), "completed_requests": 0,
              "scope": "LOCAL_ASGI_API_WITH_REAL_LOOPBACK_INFERENCE",
              "quality": "NOT_MEASURED", "energy_joules": None,
              "license_admission": "NOT_VERIFIED", "external_witness": "NOT_PERFORMED",
              "max_tokens": max_tokens, "backend_evidence": "LOCAL_DAEMON_REPORTED"}
    started = time.perf_counter()
    lifetime = ExitStack()
    stage = "SETUP"
    try:
        # This isolated process must never inherit remote receipt sinks, signing
        # secrets, grid fetches, or caller auth. Private session key stays in RAM.
        for name in ("SZL_RECEIPT_SINK", "SZL_RECEIPT_KEY_PEM", "SZL_RECEIPT_KEY_FILE",
                     "SZL_RECEIPT_GRID_CONTEXT", "SZL_ROUTER_TOKEN"):
            os.environ.pop(name, None)
        os.environ["SZL_RECEIPT_EPHEMERAL"] = "1"
        os.environ["SZL_LOCAL_OLLAMA_ENABLE"] = "1"
        from fastapi.testclient import TestClient
        from szl_router import core, receipts
        from szl_router.app import app
        backend_root = DEFAULT_BACKEND
        if isolated_cpu:
            from demo.local_worker import isolated_cpu_worker
            stage = "WORKER_STARTUP"
            worker = lifetime.enter_context(isolated_cpu_worker())
            backend_root = loopback_root(worker.root_url)
            report["worker"] = {"mode": "OWNED_CPU_PROCESS", "pid": worker.pid,
                                "version": worker.version,
                                "metadata_sha256": worker.metadata_sha256}
        report["backend_root"] = backend_root
        # Only the selected loopback provider is available inside this demo.
        core.PROVIDERS = {"local_ollama": replace(core.PROVIDERS["local_ollama"],
            base_url_default=backend_root + "/v1")}
        core._RETRY_MAX_ATTEMPTS = 1
        stage = "BACKEND_METADATA"
        report["model"] = model_identity(model, backend_root)
        report["backend"] = local_json("/api/version", backend_root)
        stage = "SOURCE_AND_SIGNING"
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                capture_output=True, text=True, check=True, timeout=5).stdout.strip()
        report["source_commit"] = commit
        report["source_checkout_clean"] = not subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"], cwd=ROOT,
            capture_output=True, text=True, check=True, timeout=5).stdout.strip()
        files = ["szl_router/core.py", "szl_router/app.py", "szl_router/receipts.py",
                 "demo/run_demo.py", "demo/fixtures/prompts.json"]
        if isolated_cpu:
            files.append("demo/local_worker.py")
        report["source_sha256"] = {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest()
                                   for f in files}
        fixtures = json.loads((ROOT / "demo/fixtures/prompts.json").read_text())
        latencies = []
        receipts.init_signing(log=lambda _: None)
        key = receipts.public_key_pem()
        if not key or not receipts.signing_state().ephemeral:
            raise ValueError("ephemeral receipt signing library unavailable")
        (output / "session.pub").write_text(key, encoding="ascii")
        with TestClient(app) as client, (output / "requests.jsonl").open("w", encoding="utf-8") as stream:
            for index in range(count):
                stage = "INFERENCE"
                remaining = deadline - (time.perf_counter() - started)
                if remaining <= 0:
                    raise TimeoutError("demo time budget exceeded")
                # Bound the synchronous upstream call by the remaining budget.
                original_chat = core.chat
                def bounded_chat(*args, **kwargs):
                    kwargs["timeout"] = min(60, remaining)
                    return original_chat(*args, **kwargs)
                core.chat = bounded_chat
                fixture = fixtures[index % len(fixtures)]
                messages = [{"role": "user", "content": fixture["prompt"]}]
                t0 = time.perf_counter()
                try:
                    response = client.post("/v1/chat/completions", json={
                        "model": "local_ollama:" + model, "messages": messages,
                        "temperature": 0, "max_tokens": max_tokens, "seed": 17,
                        "reasoning_effort": "none"})
                finally:
                    core.chat = original_chat
                stage = "RESPONSE_AND_RESIDENCY"
                body, envelope = check_response(response, model, messages, key, backend_root)
                loaded = local_json("/api/ps", backend_root)["models"]
                resident = [m for m in loaded if m.get("name") == model
                            and m.get("digest") == report["model"]["manifest_sha256"]]
                if len(resident) != 1:
                    raise ValueError("selected model digest not resident on local daemon")
                if isolated_cpu and (type(resident[0].get("size_vram")) is not int
                                     or resident[0]["size_vram"] != 0):
                    raise ValueError("owned worker did not report CPU-only residency")
                latency = (time.perf_counter() - t0) * 1000
                latencies.append(latency)
                row = {"index": index, "fixture": fixture["id"], "messages": messages,
                       "latency_ms": latency, "response": body, "envelope": envelope,
                       "resident_model": {k: resident[0].get(k) for k in
                           ("name", "digest", "size", "size_vram", "context_length")}}
                stream.write(json.dumps(row, ensure_ascii=True) + "\n")
                stream.flush()
                report["completed_requests"] = len(latencies)
        stage = "FINAL_METADATA"
        if model_identity(model, backend_root) != report["model"]:
            raise ValueError("model changed during demo")
        if time.perf_counter() - started > deadline:
            raise TimeoutError("demo time budget exceeded")
        report.update(status="PASS", fixture_count=len(fixtures), requested_requests=count,
                      signature_scope="EPHEMERAL_SESSION_ONLY", tamper_rejection="PASS",
                      latency_ms={"p50": percentile(latencies, .5), "p95": percentile(latencies, .95),
                                  "method": "nearest-rank, includes receipt validation"})
    except Exception as error:
        # Retain partial evidence; do not print upstream bodies or environment.
        report["error"] = type(error).__name__
        report["failure_stage"] = stage
    finally:
        try:
            lifetime.close()
            if "worker" in report:
                report["worker_cleanup"] = "OWNED_PROCESS_TREE_CLOSED"
        except Exception as error:
            report.update(status="FAIL", error=type(error).__name__, failure_stage="WORKER_CLEANUP")
    report["elapsed_seconds"] = time.perf_counter() - started
    if report["status"] == "PASS" and report["elapsed_seconds"] > deadline:
        report.update(status="FAIL", error="TimeoutError", failure_stage="WORKER_CLEANUP_BUDGET")
    report["artifact_sha256"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in output.iterdir() if p.is_file()}
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    digest = hashlib.sha256((output / "report.json").read_bytes()).hexdigest()
    (output / "report.sha256").write_text(digest + "\n", encoding="ascii")
    print(json.dumps({"status": report["status"], "completed_requests": report["completed_requests"],
                      "elapsed_seconds": report["elapsed_seconds"], "report_sha256": digest}))
    return 0 if report["status"] == "PASS" else 1


def verify(output, expected_digest):
    """Replay byte integrity and session signatures without any backend calls."""
    import httpx
    raw = (output / "report.json").read_bytes()
    if not expected_digest or hashlib.sha256(raw).hexdigest() != expected_digest:
        raise ValueError("report digest mismatch")
    report = json.loads(raw)
    count = report.get("requested_requests")
    if report.get("schema") != "szl.router-local-demo/v1" or type(count) is not int or not 1 <= count <= 200:
        raise ValueError("invalid report schema/count")
    if report["status"] != "PASS" or set(report["artifact_sha256"]) != {"session.pub", "requests.jsonl"}:
        raise ValueError("incomplete demo evidence")
    for name, digest in report["artifact_sha256"].items():
        if hashlib.sha256((output / name).read_bytes()).hexdigest() != digest:
            raise ValueError("artifact digest mismatch")
    key = (output / "session.pub").read_text()
    rows = [json.loads(line) for line in (output / "requests.jsonl").read_text().splitlines()]
    if len(rows) != report["completed_requests"] or len(rows) != report["requested_requests"]:
        raise ValueError("request count mismatch")
    from szl_router import receipts
    backend_root = loopback_root(report.get("backend_root", DEFAULT_BACKEND))
    for index, row in enumerate(rows):
        if row["index"] != index:
            raise ValueError("request sequence mismatch")
        response = httpx.Response(200, json=row["response"],
                                  headers={"x-szl-receipt": receipts.encode_header(row["envelope"])},
                                  request=httpx.Request("POST", "http://testserver/v1/chat/completions"))
        check_response(response, report["model"]["name"], row["messages"], key, backend_root)
        if report.get("worker", {}).get("mode") == "OWNED_CPU_PROCESS":
            resident = row.get("resident_model", {})
            if (resident.get("name") != report["model"]["name"]
                    or resident.get("digest") != report["model"]["manifest_sha256"]
                    or type(resident.get("size_vram")) is not int or resident["size_vram"] != 0
                    or report.get("worker_cleanup") != "OWNED_PROCESS_TREE_CLOSED"):
                raise ValueError("CPU worker residency/cleanup evidence mismatch")
    return {"status": "PASS", "verified_receipts": len(rows),
            "scope": "BYTE_INTEGRITY_REQUEST_BINDINGS_AND_SESSION_SIGNATURES"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="exact installed Ollama name, including tag")
    parser.add_argument("--requests", type=int, default=3)
    parser.add_argument("--output", type=Path, help="new evidence directory")
    parser.add_argument("--verify", type=Path, help="verify an existing evidence directory offline")
    parser.add_argument("--expected-report-sha256", help="independently retained digest printed by run")
    parser.add_argument("--deadline", type=float, default=300)
    parser.add_argument("--max-tokens", type=int, default=48)
    parser.add_argument("--isolated-cpu", action="store_true",
                        help="use an owned temporary CPU Ollama worker and cached weights")
    args = parser.parse_args()
    if args.verify:
        if not args.expected_report_sha256:
            parser.error("offline verification requires the independently retained --expected-report-sha256")
        try:
            print(json.dumps(verify(args.verify, args.expected_report_sha256)))
            return 0
        except Exception as error:
            print(json.dumps({"status": "FAIL", "error": type(error).__name__}))
            return 1
    if not args.model or not args.output:
        parser.error("--model and --output are required for an inference run")
    if not 1 <= args.requests <= 200 or not 0 < args.deadline <= 300 or not 1 <= args.max_tokens <= 256:
        parser.error("requests must be 1..200; deadline 0..300 seconds; max-tokens 1..256")
    return run(args.model, args.requests, args.output, args.deadline, args.max_tokens, args.isolated_cpu)


if __name__ == "__main__":
    raise SystemExit(main())
