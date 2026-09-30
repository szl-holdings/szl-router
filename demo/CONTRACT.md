# Offline router contract acceptance

This harness exercises the actual HTTP application, frontend assets,
authentication, policy refusal, ordered failover, answer receipts, tamper
rejection and the separate offline verifier. Provider responses are
**SYNTHETIC FIXTURES**. No model inference, external APIs, paid computation,
training or publication is performed. Install the pinned development
dependencies once, then run:

```text
python -m demo.run_contract_demo --output contract-report.json
```

The default is 200 completion requests. Failed checks raise errors and exit
nonzero, including with optimized Python. The child server binds loopback,
uses public fixture credentials and exits after the run. Candidate file
hashes bind the actual working tree because it may differ from HEAD. Fixture
HTTP latency is not a model throughput or quality measurement.

For a three-minute interactive rehearsal:

```text
python -m demo.run_contract_demo --serve
```

Open the printed loopback URL. Enter the displayed fixture caller value in
the caller field. Compute the plan, submit a short public prompt, then inspect
the two attempts and the unsigned receipt. Stop with Ctrl+C. Production has
no fixture environment flag; this is a separate demo entry point.

Save an independent captured `{ "request": ..., "completion": ... }` bundle
and verify it without a server:

```text
python -m router_control.verification captured-answer.json
```

CONSISTENT establishes content commitment agreement. Anyone can recompute an
unsigned receipt. It does not identify a signer, attest weights, verify a live
deployment or measure answer quality. No fallback video or seven-night
history is supplied by this harness.

The existing `demo/run_demo.py` is the separate real loopback inference lane.
Its instructions and evidence boundaries remain in `demo/README.md`.
