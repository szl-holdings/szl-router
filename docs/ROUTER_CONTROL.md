# SZL Sovereign Router

## Purpose

`szl-router` is the sovereign-first, OpenAI-compatible routing source authority for the SZL estate. It exposes deterministic policy planning and, only after explicit operator configuration, bounded chat-completion forwarding with a receipt for every successful or exhausted route.

SZL Atelier and SZL Constellation may showcase the router, its model aliases, and its evidence. They do not become the router code owner or credential holder.

## Default state

The container and application are **default deny**:

```text
SZL_ROUTER_ENABLE_EGRESS=0
```

Without a valid registry and hostname allowlist, `/api/plan` remains available for honest inspection but `/v1/chat/completions` fails closed.

## Configuration

The registry, hostname allowlist, explicit egress enablement and caller authentication must converge before a caller can invoke a provider:

```bash
export SZL_ROUTER_ALLOWED_HOSTS="provider-a.example,provider-b.example"
export SZL_ROUTER_PROVIDERS_JSON='{
  "providers": [
    {
      "id": "sovereign-a",
      "base_url": "https://provider-a.example/v1",
      "models": {"szl-default": "upstream-model-name"},
      "token_env": "PROVIDER_A_TOKEN",
      "priority": 10,
      "sovereignty": 95,
      "cost_tier": 3,
      "classifications": ["public", "internal"],
      "enabled": true
    }
  ]
}'
export PROVIDER_A_TOKEN="..."
export SZL_ROUTER_TOKEN="<separate gateway client credential>"
export SZL_ROUTER_ENABLE_EGRESS=1
```

Provider endpoints must use HTTPS, the default port, an exact hostname in `SZL_ROUTER_ALLOWED_HOSTS`, and no embedded credentials, query, fragment, literal IP, or localhost name. The configured `base_url` should include the provider's OpenAI-compatible API prefix, commonly `/v1`; the router appends `/chat/completions`.

Secrets are resolved only from each provider's named environment variable. The public registry reports `AVAILABLE` or `UNAVAILABLE`, never the variable name, endpoint, or credential value.

Callers must send `Authorization: Bearer <SZL_ROUTER_TOKEN>` on completion requests.
This credential is separate from upstream provider credentials. Missing caller
configuration returns 503; a missing or incorrect request credential returns 401
before any upstream call. Planner and source inspection remain available without
spending inference credits. Configuration validation errors never echo registry
input, which may contain accidentally supplied credentials.

## Deterministic policy

Candidates must satisfy all of the following:

1. provider enabled;
2. requested public model alias present;
3. requested data classification admitted;
4. cost tier at or below the request maximum.

Eligible candidates sort by:

```text
sovereignty DESC,
priority ASC,
cost_tier ASC,
provider_id ASC
```

This stable ordering prevents transport arrival or registry insertion order from selecting a route.

## OpenAI-compatible routes

| Route | Purpose |
|---|---|
| `GET /v1/models` | public model aliases from the validated registry |
| `POST /v1/chat/completions` | bounded, non-streaming completion forwarding |
| `GET /readyz` | control interface readiness; includes separate inference configuration state |
| `GET /readyz/inference` | 503 until registry, egress, caller token and an enabled credentialed provider are configured |
| `GET /.well-known/szl-source.json` | exact GitHub source identity; equivalent to `/api/source` |

An inference readiness 200 means `LOCAL_CONFIGURATION_ONLY`. It does not probe
the provider or prove a completed answer. Consumers must separately verify an
answer and its receipt. Operational responses use `Cache-Control: no-store`.
Set `SOURCE_REVISION` to the exact GitHub commit deployed (or `GIT_COMMIT`).
`SPACE_COMMIT_SHA` identifies a different repository and cannot establish GitHub
source identity.

The v1 receipt-verified route intentionally rejects streaming. It limits message count, individual and total content, stop sequences, output bytes, redirects, timeout, and connection pool size. `httpx` environment proxy inheritance is disabled.

Failover proceeds only when a provider credential is unavailable, a transport or response-contract error occurs, a rate limit occurs, or an upstream server fails. Ordinary upstream 4xx responses stop the route rather than silently changing providers.

## Receipts

Every successful response adds `szl_receipt` and the `X-SZL-Receipt` header. The receipt commits:

- normalized request digest;
- deterministic plan digest;
- selected provider ID;
- public and upstream model IDs;
- data classification;
- bounded attempt outcomes;
- elapsed time;
- normalized upstream response digest;
- `secret_material_recorded: false`.

An exhausted route returns a bounded failure receipt without including response bodies, URLs, tokens, environment variable names, or raw exception messages.

These `sha256` receipts bind content for replay and integrity checking; they are
unsigned and do not prove signer identity. The older `szl_router.app` application
uses a different DSSE receipt envelope. Consumers must explicitly select a
contract and must never treat a SHA256 digest as a digital signature.

## Ecosystem integration

Use `SZL_ROUTER_BASE_URL` in consuming services to identify this gateway. Keep it
separate from `A11OY_MODEL_BASE_URL`, which the older gateway consumes as an
upstream GPU address. Reusing the upstream setting for the gateway can create a
routing loop. A11oy remains the model and policy authority; configure only
reviewed model aliases, data classifications and cost tiers in this registry.
Preserve refusal and upstream failure outcomes through consumer adapters.

The restored `szl-build-env` repository owns runtime acceptance tooling and
`vsp-otel` owns its telemetry exporter. Their existence does not prove a deployed
gateway or an exported production trace. Keep source, configuration admission,
completed inference and observed telemetry as separate results.

## Local operation

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-router-control.txt
SOURCE_REVISION="$(git rev-parse HEAD)" uvicorn router_control.app:app --host 127.0.0.1 --port 7860
```

The policy planner and registry state are available at the root interface, with the API contract at `/api/docs`.

## Truth boundary

The following states remain independent:

```text
SOURCE_MERGED
≠ CONFIG_VALIDATED
≠ EGRESS_ENABLED
≠ PROVIDER_REACHABLE
≠ ANSWER_RECEIPT_EMITTED
≠ HUB_PUBLISHED
≠ RUNTIME_READY
≠ EXACT_SOURCE_READBACK_VERIFIED
```

GitHub remains canonical source authority. Hugging Face is a generated presentation/runtime target only after a canonical publisher reads back the exact Hub commit, controlled bytes, Space readiness, and `/deployment.json` source revision.
