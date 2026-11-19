# SZL robust KV-locality placement certificate (research-only)

**Source owner:** `szl-holdings/szl-router`, module
`router_control/kv_locality_certificate.py`. **Status:** PURE OFFLINE
ADVISORY, NOT DEPLOYED OR ACTIVATED. No network, provider SDK, device
query, GPU lease, model response, source attestation or write side effects.
The existing `router_control/coexistence_admission.py` is the separate
capacity/infrastructure authority; `szl_router/kv_admission.py` retains its
own single-GPU conservative page-planning contract.

## What we studied (public technical lineage)

| Engineering idea | Primary public work | SZL adaptation |
| --- | --- | --- |
| Exact page/kv memory accounting | [vLLM / PagedAttention (Kwon et al., SOSP 2023)](https://arxiv.org/abs/2309.06180); [source](https://github.com/vllm-project/vllm) | Retain existing independent conservative VRAM planner; this module does **not** estimate exact model weights or CUDA memory |
| Prefix reuse under worker locality | [SGLang / RadixAttention (Zheng et al., NeurIPS 2024)](https://arxiv.org/abs/2312.07104); [source](https://github.com/sgl-project/sglang) | Cache-hit evidence is only a conditional low-bound scenario until independently witnessed |
| Cache plus active-load decisions | [NVIDIA Dynamo KV-aware routing](https://docs.nvidia.com/dynamo/knowledge-base/concepts/system-architecture/kv-aware-routing); [source](https://github.com/ai-dynamo/dynamo) | Do not double-charge activity: input queue interval must already encompass active worker commitments |
| Event provenance and topology | [llm-d KV management](https://github.com/llm-d/llm-d/blob/main/docs/architecture/kv-management/README.md); [source](https://github.com/llm-d/llm-d) | Source/model/tokenizer/boot/prefix fenced cache event, but a matching hash is not a verified signature |
| Timeout/replay safety | [Temporal Activity retry semantics](https://docs.temporal.io/activity-definition) | No dispatch or automatic retry; a missed receipt never becomes execution authority |
| Alternative KV architectures | [DeepSeek-V3 MLA](https://arxiv.org/abs/2412.19437) | `STANDARD_FULL_ATTENTION` only; MLA/hybrid and cross-node KV transfers are explicitly unsupported |
| Model/license specialization | [LG EXAONE 4.0](https://arxiv.org/abs/2507.11407); [official source](https://github.com/LG-AI-EXAONE/EXAONE-4.0) | Do not infer hardware admission, model quality, or reuse rights from the research paper; check model licenses separately |
| Supply-chain evidence | [SLSA provenance 1.0](https://slsa.dev/spec/v1.0/provenance) | SHA-256 here is only a deterministic structural commitment, never a signed build/runtime provenance statement |

Concepts are studied and adapted; no third-party code, models, datasets, private
documents, or copyrighted source bodies were copied into the new module.

## SZL original contribution: interval-dominance / regret certificate

For each candidate worker `i`, define the declared (unverified) bounded
microsecond cost interval from same-source, same-model, same-tokenizer samples:

```text
P = prompt tokens; O = requested output tokens
a_i = declared prefill microseconds per token (positive integer)
b_i = declared decode microseconds per token (positive integer)
q_i_lower, q_i_upper = queue/load delay bounds, already including active work
0 <= h_i <= P = structurally matched, still UNSIGNED claimed prefix-hit tokens

L_i = (P - h_i) * a_i + O * b_i + q_i_lower
U_i =       P   * a_i + O * b_i + q_i_upper
```

`U_i` **never subtracts unverified cache hits**. Even a structurally
matching cache event can only reduce the hypothetical lower bound. Missing,
stale or identity-mismatched claims receive zero optimistic credit. The
module cannot independently validate a source receipt signer or determine
that purported cache blocks exist.

For two eligible candidates, let `k` be the deterministic minimax candidate
with smallest `(U_i, host_id)`. The lower bound is deliberately NOT
a tie-break input because an unsigned prefix claim can lower `L_i` without
independent provenance. `minimax_reference_host_not_route` exposes the
analysis reference even during a HOLD; it is never a dispatch destination.
Its **relative worst-case regret bound**
against the alternative `j` is:

```text
rho = max(0, U_k - L_j)
ROBUST DOMINANCE only if U_k < L_j
```

When the intervals overlap or touch, output a **HOLD**, not an invented
fastest-server label. For only one source-reported eligible worker, the
module displays a **single candidate pending owner admission**, not a
performance comparison. No candidate is emitted when no worker passes the
strict source/observation/identity/sample predicates.

This is original interval arithmetic for **visibility into uncertainty**,
not a new inference engine, GPU allocator, cryptographic signer, goodput
benchmark, production scheduler or algorithmic speedup.

## Evidence and transport boundaries

- The exact keys and bounded integer types are a deliberate fail-closed
  schema. Dates are timezone-aware; the input snapshot/worker/rate samples
  expire after 120 seconds, cache claims after 30 seconds. Future skew
  greater than five seconds is ineligible or rejected.
- Two named observation slots (`gate1`, `cuda`) are estimates only.
  Model source SHA, tokenizer SHA, manifest SHA, private boot identity,
  fresh canary and exclusive serving-owner claims are **supplied**, not
  independently attested by this code.
- Rate sample count must be at least 30, and the caller must supply
  independent declared queue lower/upper bounds; they may already include
  active prefill/decode. Do not add those tokens again or make an SLO
  or p95 claim based on per-token estimates.
- No UNKNOWN, RESTRICTED, or malformed data classification may be
  projected into an eligible result. No cache claim can override source,
  canary or model identity holds. Digests in the payload have no signer
  authority and never license training.
- The result always says `execution_authorized=false`,
  `route_changes_authorized=false`, `production_ready=false`,
  `cache_receipt_signature_verified=false`, `slo_met=null`.
  The structural SHA is domain-separated and deterministic but unsigned.
- Even if all workers appear healthy, production needs separately
  verified source/artifact/model identity, exact permitted HTTPS ingress
  and caller authentication, VRAM and disk admission, signing credentials,
  quality/license checks, authorized publication and real cloud readback.

## Independent, repeatable offline verification

From a clean checkout with Python 3.10+:

```powershell
python -m pytest -q tests/test_kv_locality_certificate.py
python -m pytest -q tests/test_salad_massed_coexistence.py
```

The tests intentionally use synthetic identity and queue data. Synthetic
workers are never live authority. No internet, CUDA or extra dependencies
are required to execute the pure calculation.