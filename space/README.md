---
title: SZL Router — Sovereign LLM Gateway
emoji: 🛰️
colorFrom: teal
colorTo: indigo
sdk: docker
app_port: 7860
pinned: true
license: apache-2.0
short_description: Sovereign-first OpenAI-compatible routing with per-answer receipts.
---

# SZL Router — flagship LLM gateway

**One OpenAI-compatible endpoint across owned compute and bounded hosted fallback, with an evidence-bearing receipt for every routed answer.**

SZL Router is the source-owned gateway for the SZL inference estate. It prefers operator-owned compute, falls back through explicitly configured hosted tiers, and records the route, model, observer frame, estimated vendor cost, and receipt state without exposing credentials or private network topology.

[**Open the public status surface**](https://szlholdings-llm-router-live.hf.space) · [**Inspect the source**](https://github.com/szl-holdings/szl-router) · [**Use the integrated A11oy view**](https://a-11-oy.com/code) · [**Verify evidence**](https://a11oy.net)

## Flagship boundary

This Space is the public product and status surface for the router. The actual gateway is built from [`szl-holdings/szl-router`](https://github.com/szl-holdings/szl-router), published separately as an OpenAI-compatible service, and configured at deployment with operator-controlled secrets.

The Space does **not** expose provider keys, private hosts, internal addresses, model weights, or private topology. A green status on this page proves only the state named by the accompanying evidence. It does not prove that every provider is configured, reachable, performant, compliant, or authorized for a consequential workload.

## Core contract

```text
request
  → deterministic intent and capability classification
  → operator-owned compute first
  → bounded free hosted fallback
  → bounded paid fallback
  → provenance block
  → signed receipt when a persistent key is armed
  → UNSIGNED-honest receipt otherwise
```

The public gateway contract is OpenAI-compatible:

```text
POST /v1/chat/completions
GET  /v1/models
GET  /v1/receipt/pubkey
POST /v1/receipt/verify
```

Logical routes:

| Route | Purpose | Default ordering |
|---|---|---|
| `szl-auto` | deterministic prompt-aware dispatch | selects a logical route, then applies its provider order |
| `szl-fast` | low-latency bounded work | owned compute → configured hosted fallback |
| `szl-large` | general synthesis and reasoning | owned compute → configured free tier → paid fallback |
| `szl-coder` | code and repository work | owned compute → configured coding-capable fallback |

Provider availability is environment-bound. Missing credentials or endpoints are skipped and recorded; they are never represented as live.

## What makes it an SZL flagship

- **Sovereign-first:** `sovereign: true` is reserved for compute the operator actually owns or controls.
- **Receipted routing:** the routing decision and serving route are carried into the answer receipt.
- **Deterministic `szl-auto`:** the same prompt characteristics produce the same logical-route decision under the same policy revision.
- **Failover with evidence:** retries, cooldown skips, provider failures, and the final serving route remain visible in the attempt trail.
- **Honest cost semantics:** hosted vendor cost may be estimated from a declared price table; unmetered electricity is never fabricated.
- **A11oy integration:** A11oy consumes and visualizes the router contract, while this repository remains the gateway source authority.

## Public evidence surface

The page reads Space-local redacted status contracts:

```text
/api/a11oy/v1/router/health
/api/a11oy/v1/router/models
/api/a11oy/v1/router/provenance
```

When a contract is unreachable, the UI uses a bundled, timestamped snapshot and labels it `LOCAL SNAPSHOT`, `STALE SNAPSHOT`, or `SNAPSHOT AGE UNKNOWN`. A reachable snapshot is not relabeled as live inference.

The public contracts expose stable opaque provider IDs and provider classes. They intentionally omit credentials, hostnames, URLs, IP addresses, private model targets, and routing secrets.

## Source and deployment identity

`GET /.well-known/szl-source.json` and `GET /api/build-info` bind the served Space to:

- GitHub repository `szl-holdings/szl-router`;
- an exact 40-character source revision;
- source path `space/`;
- the immutable Hugging Face deployment revision.

The protected publication workflow verifies the deployed file set and live readiness witness before accepting the Space as source-bound.

## Run the gateway

```bash
git clone https://github.com/szl-holdings/szl-router.git
cd szl-router
docker build -t szl-router .
docker run --rm -p 8000:8000 \
  -e SZL_ROUTER_TOKEN='set-in-a-secret-manager' \
  -e GROQ_API_KEY='optional-provider-key' \
  szl-router
```

Then point an OpenAI client at `http://localhost:8000/v1`.

No provider secret is required to inspect or test the deterministic routing and receipt contracts offline. Provider-backed inference becomes available only when an operator explicitly arms an approved route.

## Authority boundary

- A routing score is an estimate, not a quality guarantee.
- A receipt establishes scoped integrity and provenance; it does not prove factual truth.
- Lambda uniqueness remains **Conjecture 1**.
- No model, router, Space, signature, or polished interface authorizes consequential action by itself.
- Human and deployment policy remain authoritative.

---

**GitHub is the source of truth → Hugging Face is the public runtime mirror → A11oy is the integrated product interface → a11oy.net preserves proof and known bounds.**
