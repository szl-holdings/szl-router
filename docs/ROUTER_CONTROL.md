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

Three controls must converge before egress is possible:

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
export SZL_ROUTER_ENABLE_EGRESS=1
```

Provider endpoints must use HTTPS, the default port, an exact hostname in `SZL_ROUTER_ALLOWED_HOSTS`, and no embedded credentials, query, fragment, literal IP, or localhost name. The configured `base_url` should include the provider's OpenAI-compatible API prefix, commonly `/v1`; the router appends `/chat/completions`.

Secrets are resolved only from each provider's named environment variable. The public registry reports `AVAILABLE` or `UNAVAILABLE`, never the variable name, endpoint, or credential value.

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
