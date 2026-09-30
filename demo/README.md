# Local inference demo

Run real inference through the router's FastAPI API using an already installed
Ollama model. The normal route is fixed to `127.0.0.1:11434`; it has no API key
and no cloud fallback. It is disabled in normal servers unless the operator sets
`SZL_LOCAL_OLLAMA_ENABLE=1`; the demo enables it only in its isolated process.
Existing logical production routes stay unchanged.

Install the repository's normal HTTP/test dependencies and the receipt library
before going offline (the same installation used by CI). Start Ollama and choose
an exact model name from `ollama list`, including its tag. This demo never
downloads, redistributes, or admits a model license. If the shared daemon is busy,
use `--isolated-cpu` to launch a hidden temporary worker on a fresh loopback port.
It uses the installed Ollama executable and cached weights, disables cloud and GPU
execution in that child process, and closes its owned process tree on exit.
Default cache pruning is disabled in the child. Ollama may still perform its own
cache housekeeping; the demo does not claim that daemon startup is a read-only
filesystem operation.
The demo does not restart the shared daemon or edit its configuration. Ollama
itself may initialize user files during startup, so use an initialized existing
installation. Startup is bounded
to 30 seconds. Every completed request must report zero VRAM residency in this
mode; that is local daemon evidence, not independent hardware attestation.

```text
python -m pip install -e ".[test]"
python -m pip install "git+https://github.com/szl-holdings/szl-receipt.git@b33ce876e762bf502f1b48bbcfc4c51b24a352b7"
```

```text
python demo/run_demo.py --model khipu:latest --output demo-run
python demo/run_demo.py --model a11oy-mini-r2:latest --isolated-cpu --output cpu-demo-run
python demo/run_demo.py --verify demo-run --expected-report-sha256 DIGEST_PRINTED_BY_RUN
python demo/run_demo.py --model khipu:latest --requests 200 --max-tokens 4 --output benchmark-run
```

Each output directory must be new. The default run exercises three fixed prompts;
the 200-request microbenchmark repeats those three fixtures with truncated outputs.
The token limit is recorded, and this is a routing/inference transport benchmark.
Temperature is zero and seed is
17, but response determinism and model quality are not claimed. Maximum duration
for a passing run is 300 seconds; a missing model, empty output, failed call,
model identity drift, invalid receipt, or exceeded budget returns nonzero.

The demo uses the production ASGI app in-process and sends real HTTP requests to
the local model backend. It does not start a public router listener. Every request
stores the actual answer and an ECDSA-P256 receipt, verifies request/model bindings,
and checks that a changed signed payload is rejected. The session private key
exists only in memory. `session.pub` permits offline replay of signatures; this is
a session identity, not an organization signature or independent witness.

`report.json` records source commit and exact source-file hashes, the Ollama model
manifest digest before/after inference and resident digest after each call,
backend version, nearest-rank p50/p95
latency including receipt validation, and actual-byte artifact hashes.
`report.sha256` binds the report bytes; retain that digest separately if relying
on it later. Offline verification requires that independently retained digest.
Route receipts bind requests and providers, while the retained report digest
binds response bytes and the session public key. This trusts the local daemon's
model metadata; it does not independently attest hardware execution.
Verification covers integrity and signatures, not model quality or
remote deployment. Energy, license admission, and external witness remain
explicitly unmeasured/unverified. Failed runs retain their completed evidence.

Three-minute walkthrough: explain the local routing problem, run the first
command, inspect the responses and route provenance in `requests.jsonl`, then
run the offline verification command. A full estate release still needs its
own source, CI, publication, readiness, and functional acceptance evidence.
