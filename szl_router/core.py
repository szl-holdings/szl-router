"""
SZL Router — our own unified, OpenAI-compatible LLM router.

Design goals (learned from the leaders — LiteLLM, OpenRouter, RouteLLM — then
made our own, leaner):
  * ONE OpenAI-compatible surface in front of many upstreams.
  * Sovereign-first routing: try our own metal (GPU on our box) before any
    third-party grid tier, before any paid tier.
  * Honest provenance on every answer: served_by, base_url, sovereign,
    energy_source, tier, attempts. We never label a grid/free tier as sovereign.
  * Keys ONLY from the environment. Nothing secret is ever written to disk.
  * No half-state: a logical model either resolves to a working upstream or the
    call fails loud with the full attempt trail.

The call layer here is pure stdlib (urllib) on purpose, so it runs anywhere with
zero install and is trivially testable. The HTTP server wrapper lives in app.py.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Provider registry. base_url is the OpenAI-compatible root (ends before /chat).
# sovereign is TRUE only for hardware we own/control. energy_source is honest:
# "grid" for third-party clouds; "self-hosted" for our own metal (still grid
# power today — flip to "solar"/"renewable" only when that is literally true).
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Provider:
    name: str
    base_url_env: str          # env var holding base url, OR ""
    base_url_default: str      # fallback literal base url
    key_env: str               # env var holding the api key ("" = no key needed)
    sovereign: bool
    energy_source: str
    note: str = ""

    def base_url(self) -> str:
        if self.base_url_env:
            v = os.environ.get(self.base_url_env, "").strip()
            if v:
                return v.rstrip("/")
        return self.base_url_default.rstrip("/")

    def api_key(self) -> Optional[str]:
        if not self.key_env:
            return None
        return os.environ.get(self.key_env, "").strip() or None

    def available(self) -> bool:
        if not self.base_url():
            return False
        if self.key_env:
            return self.api_key() is not None
        return True


PROVIDERS: Dict[str, Provider] = {
    # --- sovereign: our own hardware (preferred) -----------------------------
    "box_gpu": Provider(
        name="box_gpu",
        base_url_env="A11OY_MODEL_BASE_URL",
        base_url_default="",
        key_env="A11OY_GPU_TOKEN",
        sovereign=True,
        energy_source="self-hosted",
        note="Tailscale Ollama on our own GPU node.",
    ),
    "nvidia_gpu": Provider(
        name="nvidia_gpu",
        base_url_env="NVIDIA_GPU_BASE_URL",
        base_url_default="",
        key_env="NVIDIA_GPU_TOKEN",
        sovereign=True,
        energy_source="self-hosted",
        note="Founder's rented NVIDIA GPU (vLLM/NIM/Ollama). Set the two envs to arm.",
    ),
    # --- free / grid tiers (third-party clouds) ------------------------------
    "groq": Provider(
        name="groq",
        base_url_env="GROQ_BASE_URL",
        base_url_default="https://api.groq.com/openai/v1",
        key_env="GROQ_API_KEY",
        sovereign=False,
        energy_source="grid",
        note="Free, very fast.",
    ),
    "nvidia_nim": Provider(
        name="nvidia_nim",
        base_url_env="NVIDIA_NIM_BASE_URL",
        base_url_default="https://integrate.api.nvidia.com/v1",
        key_env="NVIDIA_NIM_API_KEY",
        sovereign=False,
        energy_source="grid",
        note="NVIDIA-hosted NIM catalog.",
    ),
    "zhipu": Provider(
        name="zhipu",
        base_url_env="ZHIPU_BASE_URL",
        base_url_default="https://open.bigmodel.cn/api/paas/v4",
        key_env="ZHIPU_API_KEY",
        sovereign=False,
        energy_source="grid",
        note="GLM family; free flash tier.",
    ),
    "siliconflow": Provider(
        name="siliconflow",
        base_url_env="SILICONFLOW_BASE_URL",
        base_url_default="https://api.siliconflow.com/v1",
        key_env="SILICONFLOW_API_KEY",
        sovereign=False,
        energy_source="grid",
        note="Free Qwen3-8B and DeepSeek-R1 distills.",
    ),
    # --- paid grid tier (last resort) ----------------------------------------
    "moonshot": Provider(
        name="moonshot",
        base_url_env="MOONSHOT_BASE_URL",
        base_url_default="https://api.moonshot.ai/v1",
        key_env="KIMI_API_KEY",
        sovereign=False,
        energy_source="grid",
        note="Kimi K2. Paid; used as a strong fallback.",
    ),
}


# ---------------------------------------------------------------------------
# Logical model routes. Each logical name resolves to an ORDERED fallback list
# of (provider, upstream_model). Order encodes the doctrine: sovereign first,
# then free grid, then paid. Unavailable providers (no key/url) are skipped at
# call time, never faked.
# ---------------------------------------------------------------------------
Route = Tuple[str, str]

MODEL_ROUTES: Dict[str, List[Route]] = {
    # general large brain
    "szl-large": [
        ("box_gpu", "qwen2.5:32b"),
        ("nvidia_gpu", "meta/llama-3.3-70b-instruct"),
        ("groq", "llama-3.3-70b-versatile"),
        ("nvidia_nim", "meta/llama-3.3-70b-instruct"),
        ("moonshot", "kimi-k2-0905-preview"),
    ],
    # low-latency small brain
    "szl-fast": [
        ("box_gpu", "llama3.1:8b"),
        ("groq", "llama-3.1-8b-instant"),
        ("nvidia_nim", "meta/llama-3.1-8b-instruct"),
    ],
    # coding brain
    "szl-coder": [
        ("box_gpu", "qwen2.5-coder:32b"),
        ("nvidia_gpu", "qwen/qwen2.5-coder-32b-instruct"),
        ("nvidia_nim", "deepseek-ai/deepseek-coder-6.7b-instruct"),
        ("groq", "llama-3.3-70b-versatile"),
    ],
}

DEFAULT_MODEL = "szl-large"


# ---------------------------------------------------------------------------
# Provenance + result types
# ---------------------------------------------------------------------------
@dataclass
class Attempt:
    provider: str
    upstream_model: str
    ok: bool
    status: Optional[int] = None
    error: Optional[str] = None
    latency_ms: Optional[int] = None


@dataclass
class Provenance:
    served_by: Optional[str] = None        # "provider:upstream_model"
    provider: Optional[str] = None
    upstream_model: Optional[str] = None
    base_url: Optional[str] = None
    sovereign: bool = False
    energy_source: Optional[str] = None
    tier: Optional[str] = None             # sovereign | free-grid | paid-grid
    attempts: List[Attempt] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "served_by": self.served_by,
            "provider": self.provider,
            "upstream_model": self.upstream_model,
            "base_url": self.base_url,
            "sovereign": self.sovereign,
            "energy_source": self.energy_source,
            "tier": self.tier,
            "attempts": [a.__dict__ for a in self.attempts],
        }


def _tier_of(p: Provider) -> str:
    if p.sovereign:
        return "sovereign"
    if p.name == "moonshot":
        return "paid-grid"
    return "free-grid"


class RouterError(RuntimeError):
    def __init__(self, message: str, attempts: List[Attempt]):
        super().__init__(message)
        self.attempts = attempts


# ---------------------------------------------------------------------------
# Core call
# ---------------------------------------------------------------------------
def _post_chat(provider: Provider, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    url = provider.base_url() + "/chat/completions"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    # Some upstreams (e.g. Groq behind Cloudflare) reject the default urllib
    # User-Agent with HTTP 1010. Identify ourselves like a normal client.
    req.add_header("User-Agent", "szl-router/1.0")
    req.add_header("Accept", "application/json")
    key = provider.api_key()
    if key:
        req.add_header("Authorization", "Bearer " + key)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body)


def resolve_routes(model: str) -> List[Route]:
    """Map a requested model to its ordered fallback routes.

    Accepts our logical names (szl-large/...) OR a raw 'provider:model' override
    OR a bare upstream model (sent to every provider that lists it implicitly is
    NOT done — we require provider:model for raw to stay honest)."""
    if model in MODEL_ROUTES:
        return MODEL_ROUTES[model]
    if ":" in model:
        prov, _, up = model.partition(":")
        if prov in PROVIDERS:
            return [(prov, up)]
    # unknown logical model -> default brain
    return MODEL_ROUTES[DEFAULT_MODEL]


def chat(
    model: str,
    messages: List[Dict[str, Any]],
    *,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    timeout: float = 60.0,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run a chat completion through the sovereign-first fallback chain.

    Returns the upstream OpenAI-shaped response with an added
    `x_szl_provenance` block. Raises RouterError if every route fails."""
    routes = resolve_routes(model)
    prov = Provenance()
    attempts: List[Attempt] = []

    for provider_name, upstream_model in routes:
        provider = PROVIDERS.get(provider_name)
        if provider is None or not provider.available():
            attempts.append(Attempt(provider_name, upstream_model, ok=False,
                                    error="provider unavailable (no key/url)"))
            continue

        payload: Dict[str, Any] = {"model": upstream_model, "messages": messages}
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if extra:
            payload.update(extra)

        t0 = time.time()
        try:
            result = _post_chat(provider, payload, timeout)
            dt = int((time.time() - t0) * 1000)
            # An upstream can return 200 with an error body; treat missing
            # choices as a failure so we fall through honestly.
            if "choices" not in result:
                detail = str(result.get("error") or result.get("detail") or result)[:200]
                attempts.append(Attempt(provider_name, upstream_model, ok=False,
                                        status=200, error=detail, latency_ms=dt))
                continue
            attempts.append(Attempt(provider_name, upstream_model, ok=True,
                                    status=200, latency_ms=dt))
            prov.served_by = f"{provider_name}:{upstream_model}"
            prov.provider = provider_name
            prov.upstream_model = upstream_model
            prov.base_url = provider.base_url()
            prov.sovereign = provider.sovereign
            prov.energy_source = provider.energy_source
            prov.tier = _tier_of(provider)
            prov.attempts = attempts
            result["x_szl_provenance"] = prov.to_dict()
            return result
        except urllib.error.HTTPError as e:
            dt = int((time.time() - t0) * 1000)
            try:
                err_body = e.read().decode("utf-8")[:200]
            except Exception:
                err_body = str(e)
            attempts.append(Attempt(provider_name, upstream_model, ok=False,
                                    status=e.code, error=err_body, latency_ms=dt))
        except Exception as e:  # noqa: BLE001 - honest catch-all, recorded
            dt = int((time.time() - t0) * 1000)
            attempts.append(Attempt(provider_name, upstream_model, ok=False,
                                    error=f"{type(e).__name__}: {e}"[:200], latency_ms=dt))

    raise RouterError(f"all routes failed for model '{model}'", attempts)


def status() -> Dict[str, Any]:
    """Honest snapshot: which providers are armed right now."""
    out = []
    for name, p in PROVIDERS.items():
        out.append({
            "provider": name,
            "available": p.available(),
            "sovereign": p.sovereign,
            "tier": _tier_of(p),
            "energy_source": p.energy_source,
            "base_url": p.base_url() or None,
            "needs_key": bool(p.key_env),
            "note": p.note,
        })
    return {
        "providers": out,
        "logical_models": {k: v for k, v in MODEL_ROUTES.items()},
        "default_model": DEFAULT_MODEL,
    }
