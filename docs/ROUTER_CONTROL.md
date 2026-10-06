# SZL Sovereign Router

## Purpose

`szl-router` is the sovereign-first, OpenAI-compatible routing source authority for the SZL estate. It exposes deterministic policy planning and, only after explicit operator configuration, bounded chat-completion forwarding with a receipt for every successful or exhausted route.

SZL Atelier and SZL Constellation may showcase the router, its model aliases, and its evidence. They do not become the router code owner or credential holder.

## Default state

The container and application are **default deny**:

```text
SZL_ROUTER_ENABLE_EGRESS=0
```

## Separate Hosted Control Interface

The manually dispatched `publish-router-control-space.yml` workflow owns only
`SZLHOLDINGS/szl-router-control`. It runs from protected `main` after the separate
control-image publisher has produced an exact-source immutable digest. The Space
publisher verifies that digest's Cosign identity and GitHub provenance, exercises
the image, and requires anonymous registry access before publishing its pinned
Dockerfile. The existing `SZLHOLDINGS/llm-router-live` status Space has its own publisher.

Run the image publisher first, then dispatch the Space publisher with the verified
`image_digest` output. Any new main commit requires a new image publication.
`HF_TOKEN` is read from the GitHub environment; it is never a command-line argument
or part of the generated Space. Only an authenticated 404 may bootstrap the target.
Existing targets must match this publisher's complete generated file set and source
binding before an exact-parent update is admitted.

The Space forces inference egress off. A successful deployment witnesses the control
interface and `/readyz`; `/readyz/inference` must remain 503 until a separately
reviewed provider deployment is configured. It is not an inference or model-quality
claim. Publication receipts are checkpointed before each write and retain the source,
image, parent, and published revision if subsequent verification fails. A lost write
response records an `UNKNOWN` effect and requires readback before another attempt.

The initial attempt created the Space but its application upload failed README
metadata validation. `publishing/router-control-bootstrap.v1.json` records that
failed run, the exact initial parent commit, and both provider-file hashes. Only
that unchanged scaffold may be recovered; any advanced revision, changed file, or
additional file is rejected. Hub card validation now precedes target creation.

Without a valid registry, `/api/plan` remains available for honest inspection but `/v1/chat/completions` fails closed. HTTPS providers also require an exact hostname allowlist. The optional local Ollama provider has a separate fixed loopback policy.

## Configuration

The validated registry, explicit egress enablement and caller authentication must converge before a caller can invoke a provider. HTTPS providers also need an exact hostname allowlist and their named upstream credential:

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

HTTPS provider endpoints must use the default port, an exact hostname in `SZL_ROUTER_ALLOWED_HOSTS`, and no embedded credentials, query, fragment, literal IP, or localhost name. The configured `base_url` should include the provider's OpenAI-compatible API prefix, commonly `/v1`; the router appends `/chat/completions`.

Public model aliases and provider IDs remain bounded identifiers. An HTTPS upstream model name may contain namespace separators, for example `org/family/model:revision`; it is sent as a JSON model value and cannot be a URL, traversal path, or request destination. This syntax admission does not establish price, quota, terms, retention, model identity, or availability. Candidate records should remain `"enabled": false` until independently qualified. See [provider qualification and Hugging Face alignment](PROVIDER_QUALIFICATION.md).

HTTPS upstream secrets are resolved only from each provider's named environment variable. The public registry reports `AVAILABLE` or `UNAVAILABLE` for those credentials, never the variable name, endpoint, or value. The fixed loopback provider needs no upstream credential and reports `NOT_REQUIRED_LOOPBACK`.

For a model already installed on the router host, configure `provider_type: "ollama_loopback"` in the same registry. This type accepts neither `base_url` nor `token_env`. It contacts only the built-in `127.0.0.1:11434` address and requires a lowercase 64-character Ollama manifest digest for every public alias:

```json
{
  "providers": [{
    "id": "local-ollama",
    "provider_type": "ollama_loopback",
    "models": {"szl-local": "szl-khipu:latest"},
    "model_digests": {"szl-local": "<64 lowercase hex characters from a separately checked manifest>"},
    "priority": 0,
    "sovereignty": 100,
    "cost_tier": 0,
    "classifications": ["public"],
    "enabled": true
  }]
}
```

The angle-bracket text is a placeholder and must be replaced before configuration can validate. Set `SZL_ROUTER_PROVIDERS_JSON` to the completed object, `SZL_ROUTER_TOKEN` to a separate caller credential, and `SZL_ROUTER_ENABLE_EGRESS=1`. Do not put a credential or operator-selected URL in the local provider record. Obtain the digest from a current local manifest and compare its bytes and referenced weight hashes separately. An alias ending in `latest` is mutable; the digest pin is the router's refusal point when that alias moves. The daemon reports the installed and resident digests; the router does not independently attest the executing weights or license.

An operator can additionally opt one or more local aliases into **model-store byte admission** by adding `model_weight_digests` to that local provider. Each value is the lowercase SHA-256 digest of the `application/vnd.ollama.image.model` blob named by the pinned manifest. Set `SZL_ROUTER_OLLAMA_MODELS_DIR` to the absolute path of the Ollama model-store root visible to the router process (the directory containing `manifests/` and `blobs/`). For a container, mount that store read-only into the router and set the variable to the mount path. The store path stays in operator configuration and is not returned by the API.

```json
"model_weight_digests": {
  "szl-local": "<64 lowercase hex characters from the model blob>"
}
```

This option is off unless both the pin and absolute store root are configured. Before each opted-in completion, the router repeats its daemon manifest check, then freshly hashes the pinned manifest and **every** referenced config/layer blob from the local store. It accepts one model layer whose digest matches the weight pin. The read-only scan uses a 20-second budget, one concurrent reader, a 64 KiB manifest limit, at most 32 layers, and a 20 GB aggregate blob limit. A missing, malformed, changing, or mismatched file, a busy scan, or a deadline failure returns a local identity error without cloud fallback. `/api/local-models` reports only `PIN_CONFIGURED_NOT_PROBED`; its short-lived daemon inventory cache cannot authorize byte admission. A successful completion receipt records the sequential pre-request file reads separately from the daemon's pre/post digest reports. The reads are not an atomic store snapshot and the store is not rehashed after the completion. A read-only router mount prevents writes by that process; the deployment must separately prevent store mutation by Ollama or other actors during inference. File hashes do not prove that Ollama loaded those bytes into memory, used them for the completion, or has a verified model license.

The fixed outbound address constrains this router only. Before claiming an on-host deployment is private or sovereign, verify the Ollama listener's actual bind/firewall policy and its cloud-feature setting (`OLLAMA_NO_CLOUD=1` or its equivalent) on that host. The router's digest check does not attest those daemon settings. [Ollama configuration documentation](https://docs.ollama.com/faq)

The router process and Ollama must share a network namespace for that fixed address to work. A default Docker bridge container sees its own loopback, so building `Dockerfile.router-control` does not connect it to the host daemon. Any host-network or co-located deployment needs its own reviewed network policy and a live `/api/local-models` readback from inside the deployed router. A local Windows run does not prove the container or public Space can infer.

`/api/local-models` probes only configured local aliases. It admits one live tags/loaded-model probe at a time and reuses a result for at most two seconds under the same validated configuration. The response labels a fresh observation `LIVE_PROBE`, reuse `CACHED_RECENT` with observation time and age, and concurrent uncached probes `BUSY` without claiming a reachable model. The completion path always repeats its own pre/post digest checks; a cached status response cannot authorize inference.

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
The optional `required_provider_id` on plan and completion requests narrows the
candidate set to that provider while retaining the model, classification, cost,
and enabled checks. If the named provider is unavailable, the request fails
without visiting another provider. The browser's local-model choice sets this
field so choosing a local route cannot silently become a cloud request.

## OpenAI-compatible routes

| Route | Purpose |
|---|---|
| `GET /v1/models` | public model aliases from the validated registry |
| `POST /v1/chat/completions` | bounded, non-streaming completion forwarding |
| `POST /api/verify` | bounded unsigned integrity checks for completion and original request |
| `GET /version` | GitHub SHA and null aggregate model SHA; each local alias has its own configured pin |
| `GET /readyz` | control interface readiness; includes separate inference configuration state |
| `GET /readyz/inference` | 503 until registry, egress, caller token and an enabled usable provider are configured; configuration only |
| `GET /api/local-models` | live, bounded, read-only digest observations for configured local aliases |
| `GET /.well-known/szl-source.json` | exact GitHub source identity; equivalent to `/api/source` |

An inference readiness 200 means `LOCAL_CONFIGURATION_ONLY`. It does not probe
the provider or prove a completed answer. Consumers must separately verify an
answer and its receipt. Operational responses use `Cache-Control: no-store`.
Set `SOURCE_REVISION` to the exact GitHub commit deployed (or `GIT_COMMIT`).
`SPACE_COMMIT_SHA` identifies a different repository and cannot establish GitHub
source identity.

The v1 receipt-verified route intentionally rejects streaming. It limits message count, individual and total content, stop sequences, output bytes, redirects, timeout, and connection pool size. `httpx` environment proxy inheritance is disabled.
The whole provider attempt, including local metadata checks, has a 45-second wall deadline. A request has a 660-second total route deadline; the browser allows longer for its response and verification. Deadline exhaustion is retained in the bounded failure attempt trail.

Failover proceeds only when a provider credential is unavailable, a transport or response-contract error occurs, a rate limit occurs, or an upstream server fails. Ordinary upstream 4xx responses stop the route rather than silently changing providers.
For a selected local provider, an absent or changed manifest digest, or a mismatched resident digest, stops the route without sending the same prompt to a cloud provider. A cold installed model may be absent from the resident list before its first completion; successful completion requires the resident digest to match afterward. The local observation endpoint is a snapshot, so completion repeats identity checks around the provider call.
When local-store byte admission is enabled for that alias, a failed file-hash scan also stops the route before inference. The scan consumes part of the local provider's 45-second attempt deadline; a pinned local attempt that exhausts that deadline does not fall through to a cloud candidate.

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
- for local inference, the configured and daemon-reported model digest and the identity evidence state;
- when opted in, a separately labeled, pre-request local-store manifest and blob hash observation;
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
The local provider's digest and resident check bind the receipt to what the
local daemon reported. They do not make a cryptographic claim about the bytes
actually executing in hardware. License and task quality need their own
evidence before model promotion.

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
