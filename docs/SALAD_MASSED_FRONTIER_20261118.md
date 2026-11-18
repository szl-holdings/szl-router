# SZL SaladCloud × Massed A6000 Frontier — research to release

**Source owner:** szl-holdings/szl-router. Supporting authorities:
szl-serve for resident modelhost and exact fingerprints; szl-gpu-bridge
for SaladCloud observations; a11oy for governed application consumption.
**Status: SOURCE-ONLY ADVISORY.** No public gateway, GPU job, paid replica,
model promotion, cloud change, or live routing is created by this source.

## Date-bound SZL operating truth

At 2026-11-17 23:39 UTC, two existing RTX A6000 modelhosts were
inspected through source-pinned, known-host-verified SSH. Gate1 had 48
of 48 verified required placements and HTTP 200 readiness. CUDA had
45 of 45 verified placements, but HTTP 503 due to disk below the
unchanged 50-GiB reserve. Following exact-path, inode, process/mount,
tracked-file and restoration preflights, the authorized operator
reclaimed a measured 1,528,934,400 bytes from three old reproducible
build/extraction folders, without modifying weights, source, modelhost
processes or protected containers. CUDA became HTTP 200/ready,
45/45 verified, with 54,284,816,384 bytes free: only ~570 MiB over
the strict disk reserve. These are measured moments, not an SLA.

Each host answered one 8-token-maximum, fingerprint-fenced request
for resident salad-a11oy-mini-r1: 2 tokens, 225.372 ms on Gate1
and 2 tokens, 193.421 ms on CUDA. Those single canaries prove a
bounded execution with verified identity, **not** sustained throughput,
quality, energy or capacity for new concurrent traffic. The two
modelhosts have 93 placements across 48 distinct model names, with
historical cross-node fingerprint differences for seven shared names.

The separately approved Salad observer returned 139 groups from its
known default project but captured only 120 at 2026-11-17 21:47 UTC.
Other projects and real trainer heartbeat/step provenance were not
fully observed. No replica scale-up, budget, trainer restart, checkpoint
overwrite or autonomous failover is authorized by a group status.

The A11oy Space sovereign-health route reported HTTP 200 but
wired:false at 2026-11-18 00:02 UTC. A11oy's four opt-in SZL_ROUTER
configuration names were absent, and Cloudflare production scope
returned 401 for the existing tunnel read and 403 for the Access
organization. The **public application-to-A6000 path is NOT WIRED**;
do not put a raw port, SSH credential, or arbitrary tunnel URL in a
Hub Space to bypass the authorization gap.

## Leaders, source, papers and concrete SZL adoption

| Leader | GitHub source | Research/publication | Useful adoption | Not yet authorized |
| --- | --- | --- | --- | --- |
| vLLM / PagedAttention | https://github.com/vllm-project/vllm | Kwon et al., SOSP 2023, DOI https://doi.org/10.1145/3600006.3613165 | Block-aware KV budget, request token limits and identity fencing. | Do not replace current loaded Transformers/PEFT modelhost or claim vLLM speedup absent paired tests. |
| SGLang / RadixAttention | https://github.com/sgl-project/sglang | Zheng et al., NeurIPS 2024, https://proceedings.neurips.cc/paper_files/paper/2024/hash/724be4472168f31ba1c9ac630f15dec8-Abstract-Conference.html | Same-model/same-boot prefix reuse, structured output and cache-aware routing. | No inferred KV cache locality across unrelated engine revisions. |
| NVIDIA Dynamo / NIXL | https://github.com/ai-dynamo/dynamo | https://docs.dynamo.nvidia.com/dynamo/knowledge-base/concepts/system-architecture/disaggregated-serving | Separate client admission, worker discovery and prefill/decode efficiency measurement. | No NVLink/RDMA, cross-host KV transfer or PD performance assumed on the two standalone A6000 hosts. |
| llm-d 1.0 | https://github.com/llm-d/llm-d | Official production release, 2026-10-20: https://cloud.google.com/blog/topics/ai-infrastructure/llm-d-1-0-an-open-source-stack-for-distributed-inference/ | SLO-aware routing, multi-tenancy, warm-up, prefix caching and fairness. | Don't launch Kubernetes/etcd just to imitate a hyperscaler; first prove current fleet and gateway. |
| Salad Container Engine | https://github.com/SaladTechnologies/salad-cloud-sdk-python | https://salad.com/salad-container-engine/ and https://salad.com/developers/ | Independent interruptible training/eval pool; explicit replicas, health, queue, checkpoints and restart receipts. | A container state or desired replica count cannot prove training completion; no new spend without owner. |

These source/paper links are research references, not copied
implementations, measured SZL speedups, benchmark comparisons or
claims that the current fleet uses those engines.

## Mathematics: planning and falsifiable optimization, not fake measurements

**KV cache (PagedAttention-inspired):**

    B_KV = 2 * L * H_KV * D_head * bytes_per_element *
           ceil(T / block_size) * block_size * concurrent_sequences

    B_reserve = ceil(1.10 * B_KV) + workspace_MiB * 2^20
    memory_candidate iff
       current_free_MiB - engine_reserve_MiB >= ceil(B_reserve/2^20)

L is model layers; H_KV is KV heads (not total query heads for GQA);
D_head is head dimension; T is context tokens per sequence. Block
rounding and the additional 10% are explicit operator *assumptions*,
not measured allocation. Engines also allocate weights, intermediate
activations and CUDA buffers. Missing exact dimensions = HOLD.

**Prefix caching (SGLang-inspired):**

    effective_prefill_tokens = max(0, prompt_tokens - pinned_prefix_hit_tokens)
    estimated_prefill_ms =
        1000 * effective_prefill_tokens / measured_prefill_tokens_per_second

A cache hit is admissible only when it matches model fingerprint,
tokenizer and live boot ID, with an independent cache witness.
A single generation canary supplies no trustworthy p95 or throughput.

**Queue and saturation:**

    Little: observed_queue_depth ~= arrival_rate * response_time
    goodput = qualified_completed_requests_meeting_SLO / observation_seconds

These formulas require a coherent time window and exact denominators,
not default "zero" where no observations exist. A 503 and a stalled
request must never disappear from the denominator.

**Prefill/decode disaggregation vs unified (Dynamo-inspired):**

    t_split ~= t_prefill + t_KV_transfer + t_decode + t_queue
    t_unified ~= t_prefill + t_decode + t_interference

Disaggregate only after paired, same-artifact p95 TTFT/TPOT,
KV-transfer bandwidth, protected VRAM headroom, energy and goodput
prove a net benefit; for small models or poor interconnect, unified
serving can win. A network connection is not evidence of NIXL.

**Measured economic discipline:**

    verified_cost =
        sum(actual_running_seconds * independently_verified_price_per_second)
        + independently_verified_egress_and_storage

No verified price, billed duration or settlement -> cost UNAVAILABLE,
not zero. Paid Salad capacity needs a separate approved budget.

## SZL implementation and exact source boundary

The new pure Python module is:
router_control/coexistence_admission.py

The CLI is:
scripts/assess_salad_massed.py

It consumes one redacted, exact, fresh observation through a private
file and computes a **non-binding advisory**. No provider calls, model
loads or GPU scheduling occur. Its schema is
szl.salad-massed-observation/v1 and output is
szl.salad-massed-coexistence-advisory/v1.

Default pool rules:

- **Massed Gate1 and CUDA:** resident, immutable-fingerprint inference.
  A candidate requires exact model fingerprint, live modelhost ready,
  matching process/catalog boot ID, fresh canary, observed no competing
  trainer, verified exclusive serving owner, and 50-GiB + per-node
  VRAM reserves *after* the explicit KV/workspace planning envelope.
- **Salad:** independently governed, interruptible training/evaluation.
  All existing replicas and jobs are preserved. Report returned vs
  captured groups and missing projects; no automatic rescheduling or
  paid replica changes. Training completion requires process heartbeat,
  exact checkpoint bytes, durable backup, quality evaluation, rights,
  source identity, and an authorized publication.
- **Cross-cloud:** allow only PUBLIC/INTERNAL classified data by
  independent policy; no restricted/unknown egress. No automatic POST
  replay after ambiguous provider execution. Never invent a shared KV
  transfer fabric or a zero-cost remote GPU.
- **Release edge:** exact source, origin-bypass closure, Cloudflare
  denial without identity, HTTPS gateway and Hub source readback are
  separate boolean prerequisites. Even when all are true, output
  remains a CANDIDATE requiring separately admitted human-controlled
  release, not an execution authorization.

The advisory prefers verified **additional VRAM headroom**, not an
unsupported fastest-model ranking. It does not modify
router_control.app; it does not create a new endpoint, daemon,
source owner, scheduler, test cluster, registry or provider fallback.

All output includes:
execution_authorized=false; route_changes_authorized=false;
salad_scale_out_authorized=false; paid_compute_authorized=false;
cross_provider_kv_transfer_assumed=false;
cloudflare_settlement_qualified=false.

### Offline reproduction (Python 3.10+)

    python -m pytest -q tests/test_salad_massed_coexistence.py
    python scripts/assess_salad_massed.py \
        --snapshot /private/exact-observation.json \
        --output /private/new-exclusive-advisory-receipt.json

The sample tests are synthetic. Live input must retain true UTC,
source revision, actual model fingerprint, boot IDs, and conservative
KV shape; never substitute fixture numbers for provider measurements.
A CLI success only means the read-only math finished, not approval.

## Production admission sequence

1. Reverify both modelhost service boot identities, canaries,
   exact fingerprints, disk/VRAM, trainer process owners and reserve;
   preserve Gate1/CUDA listeners strictly on loopback.
2. Enumerate every authorized Salad project/group/instance and
   quota, actual trainer process, checkpoint durability and cost.
   Resolve partial 120/139 capture and other-project unknowns.
3. Recover Cloudflare-approved **read** authority for the existing
   tunnel and Access organization; inspect effective auth policies,
   verify unauthenticated denial and direct-origin bypass closure.
   No alternative token, unreviewed tunnel or DNS changes.
4. Separately admit existing router_control.app source and intended
   model alias; stage authenticated source-revision and fingerprint
   bind; configure A11oy SZL_ROUTER_* variables/secrets only through
   its authorized secret owner and normal protected release process.
5. Run one bounded source-fenced HTTPS inference and negative auth,
   wrong-fingerprint, timeouts and boot-identity tests; correlate
   operator-loopback witness, Hub deployment and public edge.
6. Admit a reviewed load envelope only afterward. Measure
   p50/p95/p99 TTFT/TPOT, errors, cost, goodput and energy against
   baseline; do not run a blind public stress flood.
7. Publish evaluated model/dataset cards and PyPI/DOI receipts only
   with exact source and cryptographic provenance. Recognize x402
   revenue only after independent settlement and request correlation.

**Current disposition: source-only readiness architecture.**
Massed compute currently works via authorized SSH; Salad group
observations are partial; A11oy public cloud-to-GPU wiring remains
unverified and blocked by the existing Cloudflare Access read denial.
