# SZL Frontier Inference — Leaders, Formulas, Source and Production Gates

Research snapshot: **2026-11-17**. Canonical SZL source: https://github.com/szl-holdings/szl-router . This is an independently authored comparison, **not** a copied kernel or a measurement of SZL inference speed. GitHub commit links below are snapshots read from the official source repositories, not necessarily the commits used to evaluate the original publications.

## Leading systems and what to learn from them

| Leader and publication | Public source (snapshot commit) | Mechanism | Adaptation / boundary |
| --- | --- | --- | --- |
| **vLLM** — Kwon et al., [PagedAttention, SOSP 2023](https://arxiv.org/abs/2309.06180), [DOI](https://doi.org/10.1145/3600006.3613165) | [vllm-project/vllm](https://github.com/vllm-project/vllm/tree/a2915755bf1dcaf80623e36d079713bfb190c1e7), Apache-2.0 | Page-based KV allocation reduces fragmentation and prevents over-reserving contiguous space. | SZL now computes a **conservative** page-rounded resident-KV estimate. This is not a new vLLM backend. |
| **SGLang** — Zheng et al., [NeurIPS 2024 structured LM programs](https://proceedings.neurips.cc/paper_files/paper/2024/hash/724be4472168f31ba1c9ac630f15dec8-Abstract-Conference.html) | [sgl-project/sglang](https://github.com/sgl-project/sglang/tree/6f35d412f2f98c922fb80e168cbf03147c6aa019), Apache-2.0 | RadixAttention reuses exact prompt prefixes, with structured decoding controls. | **No speculative prefix-reuse savings**: the planner does not discount memory without a measured cache hit ratio and validated tokenizer/model identity. |
| **NVIDIA Dynamo** — [Disaggregated inference guide](https://docs.dynamo.nvidia.com/dynamo/dev/kubernetes/disaggregated-serving/overview) | [ai-dynamo/dynamo](https://github.com/ai-dynamo/dynamo/tree/974150877594349a25f678fe938e6cc0aba41393); inspect component-specific licenses | Prefill and decode can use separate worker pools if transfer topology makes sense; KV transfer can be expensive. | **No fused GPU VRAM**, no inferred RDMA/NVLink, and no disaggregation speedup without tests. |
| **Ray Serve** — [autoscaling](https://docs.ray.io/en/latest/serve/autoscaling-guide.html), [advanced](https://docs.ray.io/en/latest/serve/advanced-guides/advanced-autoscaling.html) | [ray-project/ray](https://github.com/ray-project/ray/tree/5a7c85e58783daf8279aebf9a4348633a146e8c2), Apache-2.0 | Control replicas using measured ongoing requests and tuned per-replica queue targets. | Existing SZL mesh placement is separate from this memory estimate. A live autoscaler requires workload telemetry and hard owner quotas. |
| **FlashAttention** — Dao et al., [IO-aware exact attention](https://arxiv.org/abs/2205.14135), [FA2](https://tridao.me/publications/flash2/flash2.pdf) | [Dao-AILab/flash-attention](https://github.com/Dao-AILab/flash-attention/tree/698323347caa34174154bd39ca8d442ba4b9bf64), BSD-3-Clause | IO-aware tiling lowers high-bandwidth-memory reads/writes for attention. | Kernel choice is **not** a substitute for a KV residency budget. No kernel was installed or benchmarked by this PR. |
| **NVIDIA GenAI-Perf / AIPerf** — [reference metric formulas](https://docs.nvidia.com/aiperf/reference/ai-perf-metrics-reference) | [Triton GenAI-Perf docs](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/perf_benchmark/genai-perf-README.html) | Measure TTFT, per-token ITL, p99 latency, successful tokens/s and OOM at a fixed workload. | This planner does not fabricate inference metrics or benchmark results. |
| **Palantir Foundry / AIP / Apollo** — [Ontology](https://www.palantir.com/docs/foundry/ontology/overview) and [architecture](https://www.palantir.com/docs/foundry/architecture-center/platforms) | [official Foundry Python SDK](https://github.com/palantir/foundry-platform-python/tree/41f686a9fc44cb8341272db6430b2e011ba9bbcb), Apache-2.0 SDK; *not* open-source Foundry platform | Represent objects, logic, governed actions and their effects in operational workflows. | SZL emits typed, source-controlled, hash-verifiable **unsigned planning evidence**, not a proprietary platform-compatible Ontology or a certified action. |

No upstream source code was copied. External paper throughput claims must not be misrepresented as measured SZL throughput.

## Mathematical mechanics studied and independently adapted

### Paged attention KV allocation and a conservative memory envelope

For a **standard full-attention unsharded inference model** with L layers, H_KV attention key/value heads, D dimensions per head, S bytes per element, and physical page size P tokens:

1. `B_KV/token = 2 × L × H_KV × D × S` (K and V tensors).
2. `pages/request = ceil((T_prompt + T_max_output) / P)`.
3. `B_KV/request = pages/request × P × B_KV/token`.
4. `B_total = B_weights + B_nonKV_workspace + B_safety_reserve + concurrency × B_KV/request`.
5. `estimated_max_concurrency = floor(max(0, VRAM - weights - workspace - reserve) / B_KV/request)`.

The last page may be partly empty, so the planner reports its **exact slack tokens and bytes**. It does NOT credit prefix reuse, offloading, preemption, sharding, compression or unknown allocator savings. Model memory, workspace and reserve are all independently required declarations; unknown values must HOLD. This is **one GPU at a time**: two separate 48-GiB GPUs are not one 96-GiB GPU. Sparse/MLA/sliding-window/hybrid KV layouts require their own separately reviewed formula, not this model.

### Latency, throughput and queueing

NVIDIA defines `TTFT` as time to first token, `T_e2e` as end-to-end request latency, and `ITL = (T_e2e - TTFT) / (N_output - 1)` for `N_output > 1` (one token in first chunk assumption). For zero or one token, report `UNAVAILABLE`, not zero. For stable observed arrival rate λ and time W, Little's law gives average system concurrency `L = λ × W`; a coarse autoscaling proposal is `replicas ≈ ceil(L / target_ongoing_requests_per_replica)` subject to measured tail latency, limits and idle/peak tradeoffs. None is a deployed scheduler here. Only real traces can provide TTFT/ITL p99, throughput and failure rates.

### SZL hypothetical demonstration — not detected from hardware

Single declared 48-GiB GPU; weights 26 GiB, workspace 4 GiB, reserve 4 GiB; 32 layers, 8 KV heads, 128 head dimension, BF16 KV, 16-token page, 4,096 prompt and 1,024 maximum output tokens:

- `B_KV/token = 131,072 bytes`. 320 pages × 16 = 5,120 tokens. Each worst-case request uses **0.625 GiB KV**.
- 16 simultaneous requests use **10 GiB KV**; total **44 GiB**, 4 GiB apparent remaining.
- This declaration estimates **22** concurrent requests could fit; **24** would require **49 GiB** and must HOLD. Even the 16-request result is **ESTIMATE ONLY**, not permission to start jobs.
- Repeat separately for each GPU and validate measured weights, actual runtime memory and container limits first.

The standalone planner at `szl_router/kv_admission.py` uses exact integer arithmetic and a deterministic domain-separated SHA-256 receipt. This is an integrity hash, not a signature. Every report states `production_admission_established=false`, `no_runtime_gpu_measurement=true`, `no_latency_or_throughput_benchmark=true`, `no_oom_or_fragmentation_guarantee=true`, and `cross_gpu_vram_fused=false`.

## Operator execution, with explicit bounds

From an installed package, run:

~~~sh
szl-router-kv-plan --gpu-label node1-A6000 --vram-gib 48 --weights-gib 26 --workspace-gib 4 --reserve-gib 4 --layers 32 --kv-heads 8 --head-dim 128 --kv-dtype bf16 --page-tokens 16 --prompt-tokens 4096 --max-output-tokens 1024 --concurrent-requests 16
~~~

Or run `python -I szl_router/kv_admission.py` with the same flags from a source checkout. Exit `0` = estimate fits declared assumptions, `2` = memory estimate exceeds a device's declared VRAM, `3` = malformed/unknown/unsupported specification. Exit 0 is not an online runtime admission or a measured GPU benchmark. No network, model, device, token, payment or training job is touched.

## Production-readiness checklist and gap controls

1. Source-pin actual model architecture, tokenizer, tensor precision and KV geometry; measure live model weight memory and non-KV workspace, then replay the estimate per physical GPU. A label such as "A6000" does not prove these attributes.
2. Run reproducible streaming inference benchmarks for prompt/output mix, TTFT/ITL with valid denominators, p95/p99, TPS, cache hit rate, VRAM peak, OOM and preemption at 1/2/4/8/... concurrent requests; compare to the offline bounds.
3. Only after those controls, integrate an authenticated real-dispatch admission gate with bounded queues, measured capacity, cancellation accounting, budgeted spend and independent signed receipts.
4. For Cloudflare/PyPI/HF distribution, verify protected GitHub SHA, immutable deployment bytes, provider auth, model-eval receipts, private-data policy and actual x402 settlement. A source-only planner does not establish any cross-platform deployment.
5. Maintain the distinction between finite numerical validation, implementation quality, runtime performance, scientific proof and business conversion. Failed or unobserved gates remain explicit HOLD, not green.

**Outcome of this implementation:** one new, tested, Python-only **paged KV memory estimator** with reviewable assumptions and an unsigned digest. It does not yet schedule GPUs, launch training, enable paid inference or make the full estate production ready.
