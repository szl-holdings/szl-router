# From frontier research to SZL Khipu inference: source-to-runtime admission

**Date:** 2026-11-19 UTC. **Owner:** `szl-holdings/szl-router`. **Status:** reviewed research hypotheses and a narrow source-level alias repair, **not** installed GPU inference, training, or authority to spend.

## Measured SZL constraint, before any "frontier" claim

- The public A11oy runtime returned `/readyz` 200 on protected source `bfb64b3c6bf5a477762540a9685b4fd9524fdf76`, but `/api/a11oy/v1/ayllu/second-brain` reported `UNAVAILABLE_MODEL_TAG_MISSING`, `ready_for_grounded_navigation=false`, expected `khipu:latest`, and no observed tag. Khipu memory/retrieval availability must never be conflated with served model inference.
- The public compute-pool metadata reported 7 nodes, 5 reachable, 1 reachable sovereign GPU node, and **0 inference-verified nodes**. It is transport census, not measured model throughput.
- `SZLHOLDINGS/szl-router-control` answered `/readyz` 200 for **control-plane** reachability but `/readyz/inference` 503 with egress, auth, registry and provider unconfigured. This default-deny behavior is correct.
- Source repro at `szl-router/main@7aa1a20c9683b2e8c13ef33927a567f3b69627a2`: `router_control.app.IDENTIFIER` permits colon-tag public aliases; pure `coexistence_admission.assess` refused `khipu:latest` as `MODEL_ALIAS_INVALID` before evaluating hardware or gateway evidence. The accompanying patch admits this alias **into advisory evaluation only**; `execution_authorized=false`, `route_changes_authorized=false`, and `paid_compute_authorized=false` remain immutable results.

## Primary external systems studied and transferable hypotheses

| System / geography | First-party paper, code or spec | Relevant verified design | SZL hypothesis and falsifiable experiment |
| --- | --- | --- | --- |
| DeepSeek, China | [DeepSeek-V3 report](https://arxiv.org/abs/2412.19437) / [source](https://github.com/deepseek-ai/DeepSeek-V3) | MoE routing, MLA attention, FP8 and multi-token prediction with explicit performance measurements | On the **same permitted GPU and held-out fixtures**, compare accepted answers per measured GPU-second and memory. Do **not** infer a 671B model fits on one RTX A6000 or copy weights into training. |
| ByteDance, China | [HybridFlow paper](https://seed.bytedance.com/en/public_papers/hybridflow-a-flexible-and-efficient-rlhf-framework) / [verl](https://github.com/volcengine/verl) | Separates distributed RL computation from dependencies and dataflow | Make trainer attempt, checkpoint, immutable worker image, evaluator and durable receipt distinct objects. Never replay a failed non-idempotent action merely because the controller restarted. |
| LG AI Research, South Korea | [K-EXAONE 2.0 paper](https://arxiv.org/abs/2608.04505) / [repo](https://github.com/LG-AI-EXAONE/K-EXAONE-2.0) | Long-context/safety evaluations, upcycling and experimentally reported speculative decoding | Run exact-split, multilingual (Korean/English/Quechua as licensed and evaluated) and long-context tests with **offered/accepted denominators** before comparing model claims. Large-model availability is not local deployment. |
| TerminusDB, Ireland-founded project | [Repository](https://github.com/terminusdb/terminusdb) / [graph versioning](https://terminusdb.com/docs/knowledge-graph-database-version-control/) | Versioned, diffable graph facts with immutable commits | Extend SZL's **existing** Khipu/Lake evidence graph with source, checkpoint, permission, evaluation and receipt **identities**. Do not install a second production graph database without a workload/consistency benchmark. |
| Temporal, global | [Event history](https://docs.temporal.io/encyclopedia/event-history) | Durable event replay, explicit commands vs recorded effects | Require a completed prior-effect readback and stable idempotency identity before any interrupted paid training, publish or model promotion retry. |
| SLSA / Sigstore | [SLSA verifying artifacts](https://slsa.dev/spec/v1.2/verifying-artifacts) / [Cosign verification](https://docs.sigstore.dev/cosign/verifying/verify/) | Trusted subject digest, builder/signer identity, and checked provenance; an unsigned digest is not authenticated provenance | Bind container digest, protected source, dataset and model weight digests to separately authorized signer roots and independently repeat verification. Keys remain with their owners. |
| vLLM, global | [Production metrics](https://docs.vllm.ai/en/stable/usage/metrics/) | Actual served model performance telemetry distinct from configuration | Measure p50/p95/p99 latency, time-to-first-token, accepted tokens/second and errors for an exact bound model before GPU expansion. |

These are **concept transfers**, not copied external implementations, authorized corpus ingestion, verified local model support, or model quality claims. Inspect each dependency license and dataset rights separately; public source or a paper is not a license to redistribute data or checkpoints.

## SZL mathematical and release invariants (candidate design; not a new gate)

Use the existing immutable asset, dataset and proof authorities rather than an LLM-estimated readiness score.

Define a **non-promoting** evidence vector `E = (source, image, model, rights, training, checkpoint, evaluation, signer, runtime, owner)`. Every member has independently scoped states `VERIFIED | HOLD | UNAVAILABLE | INVALID`, an observation time and exact source/subject identity.

```text
candidate_admit(E) = VERIFIED
  only if every mandatory member is independently VERIFIED
  and all required byte identities match exact approved subjects
  and each measurement is within its freshness interval
  and no conflicting newer/older observation exists.

If any member is INVALID -> INVALID.
Else if any member is HOLD -> HOLD.
Else if any member is UNAVAILABLE -> UNAVAILABLE.
Never promote source labels, model output or an unsigned digest into signer authority.
```

An evaluation metric is defined only when its denominator is actually observed and positive. For a selected cohort `C` with genuine held-out labels, `accuracy(C) = accepted_correct(C) / offered(C)` when `offered(C) > 0`; otherwise UNAVAILABLE. Likewise `throughput_goodput = verified_correct_output_tokens / observed_GPU_seconds` only over an exact engine/model/hardware interval with reliable independent measurements. GPU energy per accepted item is meaningful only when a suitable device meter and bounded interval both exist; a device meter is not a process meter.

SZL-owned Lean/Lake A1–A5, Khipu numeric constraints, Egyptian unit fractions, Greek proof traditions, Andean cord interpretations and `szl-papers` are hypothesis-generating **source carriers**: treat manuscript claims, mathematical analogies and demonstrated formal theorems as separate evidence classes; do not claim historical translation or a Lean theorem without independently checking exact files, kernel assumptions and any `sorry`/axiom disclosures.

## Exact SZL formal and heritage sources: stop at the theorem boundary

This lane also inspected protected `szl-papers/main@7f5040f97412f078e35a5d85d51bc4ab69633ead` and `lutar-lean/main@98f1735bb23085a2979e0a22643a032ce98a27c9`. Those **source revisions** are not themselves fresh Lean kernel executions or training data permissions.

- [`Lutar/Thesis/TH_V18_04_EgyptianWeightSum.lean`](https://github.com/szl-holdings/lutar-lean/blob/98f1735bb23085a2979e0a22643a032ce98a27c9/Lutar/Thesis/TH_V18_04_EgyptianWeightSum.lean) states the rational normalization `k * (1/k) = 1` for positive integer `k`; this is an exact-arithmetic **weight identity**, not a proof that a model is trustworthy or that historical mathematics has been decoded.
- [`Lutar/Uniqueness.lean`](https://github.com/szl-holdings/lutar-lean/blob/98f1735bb23085a2979e0a22643a032ce98a27c9/Lutar/Uniqueness.lean) explicitly retains one `sorry` on the original unconditional uniqueness target. [The `szl-papers` research boundary](https://github.com/szl-holdings/szl-papers/blob/7f5040f97412f078e35a5d85d51bc4ab69633ead/bounty/README.md) states that A1–A4 uniqueness **is refuted**; even A1–A5 by themselves do not establish unconditional uniqueness. [Thesis v24](https://github.com/szl-holdings/szl-papers/blob/7f5040f97412f078e35a5d85d51bc4ab69633ead/thesis/v24/README.md) reports a *separately conditioned* result under slice multiplicativity. This document makes **no unconditional theorem or current kernel-green claim**.
- [The reviewed ancient-math source ledger](https://github.com/szl-holdings/szl-papers/blob/7f5040f97412f078e35a5d85d51bc4ab69633ead/docs/research/ANCIENT_MATH_SOURCE_LEDGER_20261118.md) distinguishes Inka khipu **cords (not codices)**, Rhind/Egyptian unit fractions, Euclidean GCD, Babylonian sexagesimal arithmetic and the separate Maya Dresden codex. Its integer/Fraction experiments are synthetic exact identities; none proves decipherment, commercial rights, language transfer, real data quality or scientific novelty.

**New falsifiable micro-experiment (proposal, not executed):** adversarially perturb one normalized weight or set one trust axis to zero in a pinned synthetic `Lambda_k` vector, then verify the exact expected output with both independent Python `fractions.Fraction` arithmetic and exact pinned Lean source. Keep a mismatched/rejected sample as a negative fixture. Compare under a fixed theorem statement, assumptions and executable `lake` environment. Do not conflate a mathematical invariant with an installed signed inference, deployed model or independent operational receipt.

## Engineering implementation and acceptance handoff

1. **Implemented here:** reconcile the read-only coexistence advisor alias syntax with `router_control.app.IDENTIFIER`, permitting `khipu:latest` while retaining exact field/length validation, source fingerprint checks, separate hardware and gateway holds, and the immutable zero-authority output.
2. **Offline regressions:** prove `khipu:latest` reaches an existing `HOLD_AUTHENTICATED_GATEWAY_AND_SOURCE_BINDING` rather than parser refusal, and cannot bypass missing model identity; prove URL-like, path, whitespace and oversized names are still rejected. CI + owner review required.
3. **Not implemented:** authenticated HTTPS inference ingress, A11oy config/secret changes, HF publisher changes, signing, GPU jobs, training, model weights, speculative decoding, automatic failover, second database, payments, or any autonomous loop. No new producer of DSSE evidence is introduced.
4. **Owner progression:** first qualify one exact co-located model with rights, hardware reservation, immutably hashed weights/manifest and signed held-out result. Then admit one bounded authenticated `/v1/models` + completion witness from the **deployed** A11oy container via existing router/Access path. Then test two-node failover, replay refusal, browser Holo degraded-mode rendering and named business SLO. Only then compare production GPU cost and model variants under approved budgets.
5. **Promotion veto:** until the model, route, signer and deployed source all independently qualify, Second Brain must continue to report `UNAVAILABLE_MODEL_TAG_MISSING` and cloud inference `503`. No badge flip, DNS/public Ollama port 11434, unreviewed tunnel, fake token, credential leak or branch-rule weakening.

**Source owner:** `szl-router` for the read-only admission contract. **Independent blockers:** `szl-holdings/a11oy#3776` (Second Brain), `szl-holdings/a11oy#3490` (execution identity) and `szl-holdings/szl-gpu-bridge#251` (Salad provider truth). Acceptance of this PR cannot close those issues.
