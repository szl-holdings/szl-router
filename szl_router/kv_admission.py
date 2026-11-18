"""SZL Paged-KV Memory Witness — conservative, offline single-GPU planning.

Published basis: Kwon et al. (SOSP 2023), PagedAttention / vLLM
  https://arxiv.org/abs/2309.06180
  https://github.com/vllm-project/vllm
and NVIDIA Dynamo's hardware-/transfer-aware admission caveats:
  https://docs.dynamo.nvidia.com/dynamo/dev/kubernetes/disaggregated-serving/overview

Original SZL formula under explicit standard full-attention KV assumptions:
    B_token = 2 * L * H_kv * D_head * dtype_bytes
    P_seq = ceil((prompt + max_output)/page_tokens)
    B_KV_total = concurrent_requests * P_seq * page_tokens * B_token
    B_total = weights + declared_workspace + declared_reserve + B_KV_total

All counts are exact nonnegative integers; a fit means ESTIMATE_ONLY, not a
GPU runtime contract. Prefix sharing and paged allocator metadata are *not*
credited as savings. Do not use this for MLA/sliding/window/hybrid KV layouts,
sharded model weights, disaggregated KV transfer, actual batching throughput,
quantization accuracy, device introspection, training, or production admission.

There is no inference/authorization/network/file-writing side effect.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
import re
import sys

SCHEMA = "szl.router.paged-kv-plan/v1"
DOMAIN = b"szl.router.paged-kv-plan/v1\x00"
GIB = 1024**3
MAX_GIB = Decimal("4096")
GIB_PATTERN = re.compile(r"(?:0|[1-9][0-9]{0,3})(?:\.[0-9]{1,3})?\Z")
KNOWN_DTYPES = {"fp32": 4, "bf16": 2, "fp16": 2, "fp8": 1}
MAX_INPUT_TOKENS = 262144
MAX_OUTPUT_TOKENS = 131072
MAX_REQUESTS = 4096


class KVPlanHold(ValueError):
    """Invalid or unsupported geometry: do not invent KV layout evidence."""


def _positive(value: int, name: str, cap: int) -> int:
    if type(value) is not int or not 1 <= value <= cap:
        raise KVPlanHold(name + "_INVALID_OR_UNBOUNDED")
    return value


def _gib_bytes(text: str, name: str, *, allow_zero: bool = False) -> int:
    if type(text) is not str or GIB_PATTERN.fullmatch(text) is None:
        raise KVPlanHold(name + "_NUMBER_INVALID")
    value = Decimal(text)
    if value > MAX_GIB or (value == 0 and not allow_zero):
        raise KVPlanHold(name + "_SIZE_INVALID")
    return int((value * GIB).to_integral_value(rounding=ROUND_CEILING))


def _ceil_div(a: int, b: int) -> int:
    return (a + b - 1) // b


@dataclass(frozen=True)
class PlanInput:
    gpu_label: str
    gpu_vram_gib: str
    model_weights_gib: str
    non_kv_workspace_gib: str
    safety_reserve_gib: str
    layers: int
    kv_heads: int
    head_dim: int
    kv_dtype: str
    page_tokens: int
    prompt_tokens: int
    max_output_tokens: int
    concurrent_requests: int


def compute_plan(spec: PlanInput) -> dict:
    """Estimate on a *single* GPU without assuming shared VRAM or measured speed."""
    if type(spec) is not PlanInput:
        raise KVPlanHold("MODEL_SPEC_INVALID")
    if (
        type(spec.gpu_label) is not str or not 1 <= len(spec.gpu_label) <= 64
        or spec.gpu_label.strip() != spec.gpu_label
        or any(ord(ch) < 33 or ord(ch) > 126 for ch in spec.gpu_label)
        or any(ch in spec.gpu_label for ch in ("\\", "/", ":", "@"))
    ):
        raise KVPlanHold("GPU_LABEL_INVALID")
    vram = _gib_bytes(spec.gpu_vram_gib, "VRAM")
    weights = _gib_bytes(spec.model_weights_gib, "WEIGHTS")
    workspace = _gib_bytes(spec.non_kv_workspace_gib, "WORKSPACE")
    reserve = _gib_bytes(spec.safety_reserve_gib, "RESERVE")

    layers = _positive(spec.layers, "LAYERS", 256)
    heads = _positive(spec.kv_heads, "KV_HEADS", 256)
    dim = _positive(spec.head_dim, "HEAD_DIM", 512)
    if type(spec.kv_dtype) is not str or spec.kv_dtype not in KNOWN_DTYPES:
        raise KVPlanHold("KV_DTYPE_UNSUPPORTED")
    dtype_bytes = KNOWN_DTYPES[spec.kv_dtype]
    page = _positive(spec.page_tokens, "PAGE_TOKENS", 4096)
    if (page & (page - 1)) != 0:
        raise KVPlanHold("PAGE_TOKENS_POWER_OF_TWO_REQUIRED")
    prompt = _positive(spec.prompt_tokens, "PROMPT_TOKENS", MAX_INPUT_TOKENS)
    output = _positive(spec.max_output_tokens, "OUTPUT_TOKENS", MAX_OUTPUT_TOKENS)
    if prompt + output > MAX_INPUT_TOKENS:
        raise KVPlanHold("TOTAL_CONTEXT_UNSUPPORTED")
    count = _positive(spec.concurrent_requests, "CONCURRENT_REQUESTS", MAX_REQUESTS)

    # Standard K/V tensor for each full-attention layer, no replication discount.
    token_bytes = 2 * layers * heads * dim * dtype_bytes
    tokens = prompt + output
    pages_per_request = _ceil_div(tokens, page)
    allocated_tokens = pages_per_request * page
    bytes_per_request = allocated_tokens * token_bytes
    kv_bytes = bytes_per_request * count
    fixed = weights + workspace + reserve
    estimated = fixed + kv_bytes
    available_for_kv = max(0, vram - fixed)
    max_concurrency = min(MAX_REQUESTS, available_for_kv // bytes_per_request)
    if fixed > vram:
        decision = "HOLD"
        reason = "FIXED_ESTIMATE_EXCEEDS_GPU_VRAM"
    elif estimated > vram:
        decision = "HOLD"
        reason = "DECLARED_REQUESTS_EXCEED_ESTIMATED_GPU_VRAM"
    else:
        decision = "ESTIMATE_FITS_NOT_RUNTIME_QUALIFIED"
        reason = "PAGED_KV_ESTIMATE_FITS_DECLARED_SINGLE_GPU_BUDGET"
    core = {
        "schema": SCHEMA,
        "model_assumption": "standard_full_attention_kv_no_sliding_or_mla",
        "gpu_label": spec.gpu_label,
        "single_gpu_vram_bytes_declared": vram,
        "weights_bytes_declared": weights,
        "non_kv_workspace_bytes_declared": workspace,
        "safety_reserve_bytes_declared": reserve,
        "layers": layers,
        "kv_heads": heads,
        "head_dim": dim,
        "kv_dtype": spec.kv_dtype,
        "dtype_bytes": dtype_bytes,
        "page_tokens": page,
        "prompt_tokens": prompt,
        "max_output_tokens": output,
        "concurrent_requests": count,
        "kv_bytes_per_token": token_bytes,
        "tokens_per_request_max": tokens,
        "pages_per_request": pages_per_request,
        "allocated_page_tokens_per_request": allocated_tokens,
        "last_page_slack_tokens_per_request": allocated_tokens - tokens,
        "last_page_slack_bytes_total": (allocated_tokens - tokens) * token_bytes * count,
        "kv_bytes_per_request_allocated": bytes_per_request,
        "total_kv_cache_bytes": kv_bytes,
        "estimated_fixed_bytes": fixed,
        "total_estimated_bytes": estimated,
        "estimated_remaining_bytes": vram - estimated,
        "estimated_max_concurrent_requests_same_shape": max_concurrency,
        "decision": decision,
        "reason": reason,
        "no_runtime_gpu_measurement": True,
        "no_oom_or_fragmentation_guarantee": True,
        "no_kv_prefix_reuse_discount": True,
        "no_latency_or_throughput_benchmark": True,
        "cross_gpu_vram_fused": False,
        "per_gpu_placement_only": True,
        "neural_training_performed": False,
        "production_admission_established": False,
        "signature_verified": False,
    }
    canonical=json.dumps(core,sort_keys=True,separators=(",",":"),
                         allow_nan=False,ensure_ascii=True).encode("ascii")
    return {**core,"sha256":hashlib.sha256(DOMAIN+canonical).hexdigest()}


def main(argv: list[str] | None = None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gpu-label",required=True)
    parser.add_argument("--vram-gib",required=True)
    parser.add_argument("--weights-gib",required=True)
    parser.add_argument("--workspace-gib",required=True)
    parser.add_argument("--reserve-gib",required=True)
    parser.add_argument("--layers",type=int,required=True)
    parser.add_argument("--kv-heads",type=int,required=True)
    parser.add_argument("--head-dim",type=int,required=True)
    parser.add_argument("--kv-dtype",required=True)
    parser.add_argument("--page-tokens",type=int,required=True)
    parser.add_argument("--prompt-tokens",type=int,required=True)
    parser.add_argument("--max-output-tokens",type=int,required=True)
    parser.add_argument("--concurrent-requests",type=int,required=True)
    args=parser.parse_args(argv)
    try:
        plan=compute_plan(PlanInput(
            gpu_label=args.gpu_label,gpu_vram_gib=args.vram_gib,
            model_weights_gib=args.weights_gib,
            non_kv_workspace_gib=args.workspace_gib,safety_reserve_gib=args.reserve_gib,
            layers=args.layers,kv_heads=args.kv_heads,head_dim=args.head_dim,
            kv_dtype=args.kv_dtype,page_tokens=args.page_tokens,
            prompt_tokens=args.prompt_tokens,max_output_tokens=args.max_output_tokens,
            concurrent_requests=args.concurrent_requests
        ))
    except KVPlanHold as exc:
        print(json.dumps({"schema":SCHEMA,"decision":"HOLD",
                          "reason":str(exc),"production_admission_established":False}))
        return 3
    print(json.dumps(plan,sort_keys=True))
    return 2 if plan["decision"]=="HOLD" else 0


if __name__=="__main__":
    sys.exit(main())
