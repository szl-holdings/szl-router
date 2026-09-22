<!-- SPDX-License-Identifier: Apache-2.0 -->
# Forge dual-GPU mesh: bounded local reference

`examples/forge-mesh-router.py` is a Python standard-library reference for
**direct local inference clients and two already-authorized local workers**.
It is not the production `szl-router` service, a public gateway, a multi-user
identity system, a model-admission gate, or a replacement for router-control.
Importing the file does not bind a listener or contact a worker.

Existing owner-laptop and training pauses remain in force. Source tests below
use synthetic loopback peers, not Ollama, actual models, the owner laptop, or
provider services. This change does not install/restart a service, alter
Tailscale/firewall rules, clear a recovery claim, or grant permission to run
an outstanding once-only experiment. A passing local test is not deployment.

## Intentional compatibility boundary

The listener binds **127.0.0.1:11500 only**. Workers default to
127.0.0.1:11434 and 127.0.0.1:11435. `FORGE_GPU_HOST`, when explicitly supplied,
must be a literal loopback address; DNS names, remote/tailnet addresses and
IPv6 zone identifiers are rejected before a listener is started.

This intentionally removes the old all-interface/remote-host example contract.
Do not use this script as a network service, widen its bind, expose it through
a tunnel, or remove its client checks to restore that contract. A real remote
service needs the existing owner's separate identity, TLS, authorization,
resource, data-rights and deployment acceptance work. Loopback is an OS trust
boundary, **not** isolation from another local process or another local user.

Direct HTTP/1.1 clients must send the exact local Host/port and no Origin,
Sec-Fetch-Site, Cookie, Authorization, Proxy-Authorization, Expect or Upgrade
headers. Browser-originated requests and credential forwarding are not part
of this example. SDKs which always attach a dummy API-key Authorization header
are incompatible unless that header can be omitted. No key is created here.

## Allowed operations

GET and HEAD: `/api/version`, `/api/tags`, `/v1/models`.

POST: `/api/chat`, `/api/generate`, `/api/embed`, `/api/embeddings`,
`/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/api/show`.

Only exact paths are accepted: no queries, absolute targets, encoded or
normalized aliases. Model pull/create/copy/delete and arbitrary write routes
are not proxied. A POST needs one Content-Length, application/json (optionally
`; charset=utf-8`), a strict UTF-8 JSON object and a bounded nonempty model name.
Duplicate keys, non-finite numbers, excessive depth/numeric tokens and malformed
Unicode are rejected. This validates framing, not model availability or rights.

`GET /mesh/status` returns only the local in-flight counters, reference scope,
`worker_health=NOT_PROBED` and `execution_authority=NONE`. It is not worker
liveness, model readiness, a source witness or an operational certificate.
Backend addresses and the old `X-Forge-Backend` header are not exposed.

## Transport contract and finite budgets

| Boundary | Limit |
|---|---:|
| Concurrent accepted clients | 8 |
| Raw request/upstream headers | 32 KiB each |
| Request JSON body | 1 MiB |
| Relayed response body | 8 MiB |
| Header phase, wall time | 5 seconds |
| Request-body phase, wall time | 5 seconds |
| Complete upstream phase, wall time | 30 seconds |
| Individual connection attempt | at most 3 seconds |
| Backend attempts | at most 2; fallback only before dispatch |

Slow-drip reads do not reset the wall-clock budget. Timers close relevant
sockets; input length, response bytes, JSON structure and accepted threads are
bounded separately. This is not a general hard CPU/memory sandbox. Saturated
connections are closed rather than adding unbounded waiting threads.

Embeddings prefer the iGPU; other operations use least in-flight connections,
with dGPU preferred on a tie. Selection and reservation share one lock. Only
an explicit connection failure **before any request bytes may have been sent**
permits one fallback. Any failure during sending or response handling is not
replayed, including failure before response headers. A 429/503 or another
upstream status is not retry authority. Generic failure responses retain a
bounded worker-label/attempt-state trail, never prompt content or raw errors.
A failed or interrupted generation must not be blindly resubmitted by a client.

Only JSON, NDJSON and event-stream responses are relayed. Redirects, malformed
or conflicting framing, compressed responses, and empty/partial-success status
contracts are rejected. Response headers are rebuilt, not copied wholesale.
Streaming uses incremental reads. A known length mismatch, response limit or
transport failure closes the downstream stream **without** a terminal chunk,
so it cannot masquerade as a successfully completed chunked HTTP response.
Clean HTTP EOF still does not prove that a model emitted a semantically complete
answer or an application terminal event. Clients must validate their protocol.

## Reproduce source tests without models

From the repository root, in its normal test environment:

```sh
python -m pytest -q test_forge_mesh_example.py test_forge_mesh_example_safety.py
python -O -m pytest -q test_forge_mesh_example.py test_forge_mesh_example_safety.py
```

The existing import/routing test remains unchanged. The new suite uses bounded
synthetic objects and actual ephemeral loopback HTTP peers to exercise request
consumption followed by disconnect, partial/streaming responses, wall deadlines,
framing, local-client constraints, cleanup and concurrency limits. No upstream
weights, GPU, credentials, training or paid service are needed. Run the whole
repository suite and existing scanners on the exact candidate separately.

The original example at `9f393505760b1f4d020dd5a84cd6a2d12a7c2386`
(blob `6654c70449a03c53073ca782d72126bb4d66e493`) is the predecessor control.
Its earlier parse repair and the public review remain in PR #61 history. Normal
source review/current-base checks and deployment controls still apply; do not
restore unsafe replay or all-interface binding as an undocumented rollback.

## Historical hardware report, not a new observation

The prior version of this document reported June 14, 2026 measurements for
qwen2.5-coder:7b on RTX 5050 and Intel Arc 140T: 37.8 versus 15.2 tokens/s;
dGPU 0.619 J/token, 23.4 W mean/38.8 W peak, 9.84 W idle, and a 400-token
answer in 10.6 seconds at 247.6 J. It described approximately 5 Hz NVML
sampling, but did not isolate Intel power. Those historical source claims have
**not been remeasured or independently verified by this repair**. Historical
startup-task, service-active and economic claims are not current device status.
The original document and all source history remain available at the exact
predecessor revision. Do not reactivate its tasks from these historical claims.

## Primary rationale

Python's documentation warns that `http.server` is not recommended for
production and implements only basic security checks:
https://docs.python.org/3/library/http.server.html

HTTP retry semantics distinguish a failure before sending from uncertainty
after dispatch; a proxy must not automatically retry non-idempotent requests:
https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2.2
