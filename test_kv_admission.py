"""Offline, negative and exact-integer paged KV memory planning controls.

This never contacts a GPU, model endpoint, cloud provider, or training system.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from szl_router import kv_admission as kv


ROOT = Path(__file__).resolve().parent
SCENARIO = kv.PlanInput(
    gpu_label="owner-A6000-illustration",
    gpu_vram_gib="48",
    model_weights_gib="26",
    non_kv_workspace_gib="4",
    safety_reserve_gib="4",
    layers=32,
    kv_heads=8,
    head_dim=128,
    kv_dtype="bf16",
    page_tokens=16,
    prompt_tokens=4096,
    max_output_tokens=1024,
    concurrent_requests=16,
)


def test_basic_known_geometry_exact_bytes_and_no_claims():
    p = kv.compute_plan(SCENARIO)
    assert p["decision"] == "ESTIMATE_FITS_NOT_RUNTIME_QUALIFIED"
    assert p["gpu_label"] == "owner-A6000-illustration"
    assert p["single_gpu_vram_bytes_declared"] == 48 * kv.GIB
    assert p["estimated_fixed_bytes"] == 34 * kv.GIB
    assert p["kv_bytes_per_token"] == 2 * 32 * 8 * 128 * 2 == 131072
    assert p["pages_per_request"] == 320
    assert p["allocated_page_tokens_per_request"] == 5120
    assert p["kv_bytes_per_request_allocated"] == 671088640
    assert p["total_kv_cache_bytes"] == 10 * kv.GIB
    assert p["total_estimated_bytes"] == 44 * kv.GIB
    assert p["estimated_remaining_bytes"] == 4 * kv.GIB
    assert p["estimated_max_concurrent_requests_same_shape"] == 22
    assert p["last_page_slack_bytes_total"] == 0
    assert p["production_admission_established"] is False
    assert p["no_runtime_gpu_measurement"] is True
    assert p["no_oom_or_fragmentation_guarantee"] is True
    assert p["no_latency_or_throughput_benchmark"] is True
    assert p["cross_gpu_vram_fused"] is False
    assert p["per_gpu_placement_only"] is True
    assert p["signature_verified"] is False


def test_requested_concurrency_above_estimated_bound_holds():
    p = kv.compute_plan(replace(SCENARIO, concurrent_requests=24))
    assert p["decision"] == "HOLD"
    assert p["reason"] == "DECLARED_REQUESTS_EXCEED_ESTIMATED_GPU_VRAM"
    assert p["total_estimated_bytes"] == 49 * kv.GIB
    assert p["estimated_remaining_bytes"] == -1 * kv.GIB
    assert p["estimated_max_concurrent_requests_same_shape"] == 22


def test_fixed_memory_overflow_holds_even_for_one_request():
    p = kv.compute_plan(replace(
        SCENARIO, model_weights_gib="43", non_kv_workspace_gib="4",
        safety_reserve_gib="4", concurrent_requests=1
    ))
    assert p["decision"] == "HOLD"
    assert p["reason"] == "FIXED_ESTIMATE_EXCEEDS_GPU_VRAM"
    assert p["estimated_max_concurrent_requests_same_shape"] == 0


def test_page_rounding_counts_slack_exactly():
    p = kv.compute_plan(replace(
        SCENARIO, prompt_tokens=130, max_output_tokens=1, concurrent_requests=1
    ))
    assert p["tokens_per_request_max"] == 131
    assert p["pages_per_request"] == 9
    assert p["allocated_page_tokens_per_request"] == 144
    assert p["last_page_slack_tokens_per_request"] == 13
    assert p["last_page_slack_bytes_total"] == 13 * 131072


def test_ceil_and_integer_arithmetic_never_rounds_away_a_page():
    for prompt in range(1, 100):
        for output in (1, 16, 33):
            expected_pages = sum(1 for _ in range(
                0, prompt + output, SCENARIO.page_tokens))
            p = kv.compute_plan(replace(
                SCENARIO, prompt_tokens=prompt,
                max_output_tokens=output, concurrent_requests=2
            ))
            assert p["pages_per_request"] == expected_pages
            assert p["total_kv_cache_bytes"] == (
                2 * expected_pages * SCENARIO.page_tokens * p["kv_bytes_per_token"]
            )


@pytest.mark.parametrize("dtype,bytes_per_element", [
    ("fp8", 1), ("bf16", 2), ("fp16", 2), ("fp32", 4)
])
def test_kv_dtype_bytes_explicit_not_model_quality(dtype, bytes_per_element):
    p = kv.compute_plan(replace(SCENARIO, kv_dtype=dtype))
    assert p["dtype_bytes"] == bytes_per_element
    assert p["kv_bytes_per_token"] == 2 * 32 * 8 * 128 * bytes_per_element
    assert p["neural_training_performed"] is False


def test_paged_floor_and_peak_budget_invariant():
    for concurrency in range(1, 41):
        p = kv.compute_plan(replace(SCENARIO, concurrent_requests=concurrency))
        assert p["total_estimated_bytes"] == (
            p["estimated_fixed_bytes"] + p["total_kv_cache_bytes"]
        )
        assert p["total_kv_cache_bytes"] == (
            concurrency * p["kv_bytes_per_request_allocated"]
        )
        fits = p["total_estimated_bytes"] <= p["single_gpu_vram_bytes_declared"]
        assert (p["decision"] == "ESTIMATE_FITS_NOT_RUNTIME_QUALIFIED") == fits


def test_model_geometry_explicitly_bounds_kv_heads_dimension_and_context():
    invalid = [
        dict(layers=0), dict(layers=True), dict(layers=257),
        dict(kv_heads=0), dict(kv_heads=-1), dict(kv_heads=257),
        dict(head_dim=0), dict(head_dim=513),
        dict(prompt_tokens=0), dict(prompt_tokens=True),
        dict(prompt_tokens=kv.MAX_INPUT_TOKENS + 1),
        dict(max_output_tokens=0), dict(max_output_tokens=kv.MAX_OUTPUT_TOKENS + 1),
        dict(prompt_tokens=kv.MAX_INPUT_TOKENS, max_output_tokens=1),
        dict(concurrent_requests=0), dict(concurrent_requests=4097),
        dict(concurrent_requests=True), dict(page_tokens=0),
        dict(page_tokens=24), dict(page_tokens=8192),
        dict(page_tokens=True), dict(kv_dtype="unknown"), dict(kv_dtype="mla"),
        dict(kv_dtype="BF16"),
    ]
    for values in invalid:
        with pytest.raises(kv.KVPlanHold):
            kv.compute_plan(replace(SCENARIO, **values))


def test_absent_or_ambiguous_gpu_memory_budgets_do_not_assume_free():
    for field in ("gpu_vram_gib", "model_weights_gib",
                  "non_kv_workspace_gib", "safety_reserve_gib"):
        for value in ("", " ", "0", "-1", "+1", "NaN", "Inf", "0.0000",
                      "48e0", "01", "4096.001", "9999", None, 10, True):
            with pytest.raises(kv.KVPlanHold):
                kv.compute_plan(replace(SCENARIO, **{field: value}))


def test_gpu_label_and_spec_type_rejected():
    for label in ("", " ", "/etc/passwd", "a/b", "C:\\file", "a@b",
                  "\n", "a" * 65, None, 1, True, "é"):
        with pytest.raises(kv.KVPlanHold):
            kv.compute_plan(replace(SCENARIO, gpu_label=label))
    with pytest.raises(kv.KVPlanHold):
        kv.compute_plan({"spec": "not-typed"})


def test_domain_separated_digest_replays_and_changes_when_inputs_change():
    a = kv.compute_plan(SCENARIO)
    b = kv.compute_plan(SCENARIO)
    assert a == b
    core = {key: value for key, value in a.items() if key != "sha256"}
    expected = hashlib.sha256(kv.DOMAIN + json.dumps(
        core, sort_keys=True, separators=(",",":"),
        allow_nan=False, ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    assert a["sha256"] == expected
    p2 = kv.compute_plan(replace(SCENARIO, max_output_tokens=1025))
    assert p2["sha256"] != a["sha256"]


def _run_cli(extra: list[str]):
    args = [
        sys.executable, "-I", str(ROOT / "szl_router" / "kv_admission.py"),
        "--gpu-label", SCENARIO.gpu_label,
        "--vram-gib", SCENARIO.gpu_vram_gib,
        "--weights-gib", SCENARIO.model_weights_gib,
        "--workspace-gib", SCENARIO.non_kv_workspace_gib,
        "--reserve-gib", SCENARIO.safety_reserve_gib,
        "--layers", str(SCENARIO.layers),
        "--kv-heads", str(SCENARIO.kv_heads),
        "--head-dim", str(SCENARIO.head_dim),
        "--kv-dtype", SCENARIO.kv_dtype,
        "--page-tokens", str(SCENARIO.page_tokens),
        "--prompt-tokens", str(SCENARIO.prompt_tokens),
        "--max-output-tokens", str(SCENARIO.max_output_tokens),
        "--concurrent-requests", str(SCENARIO.concurrent_requests),
        *extra,
    ]
    return subprocess.run(args, cwd=ROOT, capture_output=True,
                          text=True, timeout=10, check=False)


def test_cli_default_is_estimation_only():
    response = _run_cli([])
    assert response.returncode == 0, response.stderr
    out = json.loads(response.stdout)
    assert out["decision"] == "ESTIMATE_FITS_NOT_RUNTIME_QUALIFIED"
    assert out["production_admission_established"] is False


def test_cli_rejects_insufficient_declared_gpu_memory():
    response = _run_cli(["--concurrent-requests", "24"])
    assert response.returncode == 2, response.stderr
    assert json.loads(response.stdout)["reason"] == (
        "DECLARED_REQUESTS_EXCEED_ESTIMATED_GPU_VRAM"
    )


def test_cli_rejects_missing_or_unknown_gpu_layout():
    response = _run_cli(["--kv-dtype", "mla"])
    assert response.returncode == 3, response.stderr
    assert json.loads(response.stdout)["reason"] == "KV_DTYPE_UNSUPPORTED"


def test_no_source_side_effects_network_or_gpus_needed():
    # Module is standard-library only; no torch, NVML, vLLM import.
    source = (ROOT / "szl_router/kv_admission.py").read_text(encoding="utf8")
    assert "import torch" not in source
    assert "import pynvml" not in source
    assert "import requests" not in source
    assert "import vllm" not in source
    assert "urllib.request" not in source
    assert "from pathlib import Path" not in source


def test_zero_token_reuse_discount_for_shared_prefix_is_not_claimed():
    # Every request pays its own allocated page count, no speculative savings.
    p = kv.compute_plan(replace(
        SCENARIO, prompt_tokens=16, max_output_tokens=1,
        concurrent_requests=10
    ))
    assert p["total_kv_cache_bytes"] == (
        10 * p["kv_bytes_per_request_allocated"]
    )
    assert p["no_kv_prefix_reuse_discount"] is True
