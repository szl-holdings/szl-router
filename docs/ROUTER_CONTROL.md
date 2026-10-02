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
| `POST /api/verify` | bounded unsigned integrity checks for completion and original request |
| `GET /version` | GitHub SHA and null model SHA for mutable aliases |
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

Send `{ "completion": <full response>, "request": <original request> }` to
`/api/verify`. It checks receipt, response and normalized request digests.
Supplying the original `X-SZL-Receipt` header also checks its binding.
CONSISTENT returns 200; DIVERGENT returns 422. Invalid input returns a sanitized
422 and bodies above 3 MB return 413. Byte, depth and node bounds also apply
before the gateway emits a successful completion. Each provider attempt has
an enforced wall deadline and timeout failure remains in the attempt trail.

Verify the same bundle without a server:

```text
python -m router_control.verification captured-answer.json
```

The CLI returns 0 for consistent, 1 for divergent and 2 for invalid input.
Both paths report `UNSIGNED_HONEST` and `identity_verified: false`. Anyone can
recompute unsigned hashes; consistency does not attest weights, provider
identity, plan policy or answer quality. `/version` preserves an unavailable
model SHA until an immutable loaded artifact is actually attested.

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

The interface sends completions using the current model, classification and
cost policy. The separate caller credential is cleared from its password field
on dispatch and never persisted. Credential submission requires a secure
browser context. Control readiness and inference configuration remain separate;
configuration admission does not become a completed inference witness.

Answers, refusals, attempts and verification failures render as text. The
browser preserves original completion JSON for verification, including large
integers and numeric forms that JavaScript would otherwise rewrite. Content
consistency requires receipt, response, request and original header checks.
The interface discloses unsigned trust and missing identity verification.

The installed package includes the control gateway and its frontend assets.
Serve it with `uvicorn router_control.app:app`; the installed
`szl-router-control-verify` command verifies captured bundles. CI builds and
installs a wheel away from the checkout to qualify its API, static assets
and verifier entry point.

## Control image publication

After a protected `main` merge, the owner may dispatch
`.github/workflows/publish-router-control.yml` on `main`. The workflow checks
the selected commit against the current protected head, runs the control
contracts, builds `Dockerfile.router-control` with that commit as its source
revision, and exercises the running non-root container with networking disabled.
Only then does it publish a distinct `ghcr.io/szl-holdings/szl-router-control`
image. The tag contains the full source commit and workflow run identity so a
retry does not replace an earlier tag. The uploaded
`router-control-image-receipt.json` records the registry manifest digest and
the result of pulling and exercising that exact digest. The workflow creates
GitHub build provenance, signs the digest with Cosign keyless identity, and
verifies both before issuing a release receipt. It rereads protected `main`
after those checks; a superseded image is recorded but the workflow fails and
its receipt is not eligible for promotion. Deploy using the digest
reference in the receipt; the tag is a locator, not the immutable identity.

The image defaults to `SZL_ROUTER_ENABLE_EGRESS=0` and embeds the source commit
in its image configuration and OCI revision label. Publication does not set
provider credentials, enable egress, create an HTTPS host, or attest a live
answer. The old `ghcr.io/szl-holdings/szl-router` image remains a separate
application. Read `/deployment.json` on the eventual host and compare its
source revision with the receipt before qualifying inference there. Also
verify that the running host uses the receipt's image digest: a runtime
environment override can change what `/deployment.json` reports.

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
