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

import hashlib
import http.client
import io
import json
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import spend_guard  # SZL Sovereign Ops: paid-tier spend cap + kill-switch


# ---------------------------------------------------------------------------
# Provider registry. base_url is the OpenAI-compatible root (ends before /chat).
# sovereign is TRUE only for hardware we own/control. energy_source is honest:
# "grid" for third-party clouds; "self-hosted" for our own metal (still grid
# power today — flip to "solar"/"renewable" only when that is literally true).
# ---------------------------------------------------------------------------
def _normalize_base(url: str) -> str:
    """Normalize an OpenAI-compatible base URL.

    A bare host (scheme://host:port with no path) gets `/v1` appended — this is
    what Ollama / vLLM expose. URLs that already carry a path are left as-is."""
    u = url.strip().rstrip("/")
    if not u:
        return ""
    after = u.split("://", 1)[-1]
    if "/" not in after:  # no path component at all -> bare host:port
        return u + "/v1"
    return u


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
                return _normalize_base(v)
        return _normalize_base(self.base_url_default) if self.base_url_default else ""

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
    "omen_gpu": Provider(
        name="omen_gpu",
        base_url_env="OMEN_GPU_BASE_URL",
        base_url_default="",
        key_env="OMEN_GPU_TOKEN",
        sovereign=True,
        energy_source="self-hosted",
        note="Always-on HOME node (OMEN RTX 4060 Ti 8GB) on the founder tailnet. "
             "Set OMEN_GPU_BASE_URL=http://<omen-host>:11434/v1 to arm. Preferred "
             "for low-latency small jobs + embeddings so the TRAVELING laptop is not "
             "the sole worker. Each sovereign node is a SEPARATE worker — placement + "
             "sequential failover only; VRAM is NEVER fused/combined.",
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
    "cerebras": Provider(
        name="cerebras",
        base_url_env="CEREBRAS_BASE_URL",
        base_url_default="https://api.cerebras.ai/v1",
        key_env="CEREBRAS_API_KEY",
        sovereign=False,
        energy_source="grid",
        note="Free tier ~1M tokens/day, ultra-fast, no card. Dormant until "
             "CEREBRAS_API_KEY is armed (skipped automatically while unset).",
    ),
    "openrouter": Provider(
        name="openrouter",
        base_url_env="OPENROUTER_BASE_URL",
        base_url_default="https://openrouter.ai/api/v1",
        key_env="OPENROUTER_API_KEY",
        sovereign=False,
        energy_source="grid",
        note="Aggregator with :free model variants (DeepSeek-R1, Qwen3). Dormant "
             "until OPENROUTER_API_KEY is armed (skipped automatically while unset).",
    ),
    "google": Provider(
        name="google",
        base_url_env="GEMINI_BASE_URL",
        base_url_default="https://generativelanguage.googleapis.com/v1beta/openai",
        key_env="GEMINI_API_KEY",
        sovereign=False,
        energy_source="grid",
        note="Gemini free tier via AI Studio (2.5 Pro/Flash). HONEST CAVEAT: the "
             "free tier may use inputs to improve Google's models — keep sovereign / "
             "open-weight routes primary for sensitive data. Dormant until "
             "GEMINI_API_KEY is armed (skipped automatically while unset).",
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
    # general brain. Sovereign-first: our own GPU (llama3.1:8b on the laptop)
    # is preferred even though it is smaller than the 70B grid models — running
    # on our own metal is the point. Grid 70Bs are the fallback if the GPU is off.
    "szl-large": [
        # sovereign FIRST (doctrine unchanged): our own metal, even if smaller.
        ("box_gpu", "llama3.1:8b"),
        ("nvidia_gpu", "llama3.1:8b"),
        ("omen_gpu", "llama3.1:8b"),
        # free-grid, frontier-class FIRST: DeepSeek-R1 (open-weight, MIT) is a
        # stronger reasoner than Llama-70B and Groq serves it free today; NVIDIA
        # NIM hosts the full R1. Then ultra-fast Cerebras, OpenRouter :free R1 and
        # Gemini free — each skipped automatically until its key is armed.
        ("groq", "deepseek-r1-distill-llama-70b"),
        ("nvidia_nim", "deepseek-ai/deepseek-r1"),
        ("cerebras", "gpt-oss-120b"),
        ("openrouter", "qwen/qwen3-next-80b-a3b-instruct:free"),
        ("google", "gemini-2.5-flash"),
        # reliable 70B grid fallback, then paid last-resort (unchanged).
        ("groq", "llama-3.3-70b-versatile"),
        ("nvidia_nim", "meta/llama-3.3-70b-instruct"),
        ("moonshot", "kimi-k2-0905-preview"),
    ],
    # low-latency small brain. OFFLOAD doctrine: prefer the always-on HOME node
    # (omen_gpu) FIRST for small/fast jobs so the TRAVELING Blackwell laptop
    # (box_gpu) is not the sole worker. If OMEN is not armed/reachable this
    # falls through honestly to the laptop, then the free grid. Each is a
    # separate sovereign worker (sequential failover, never fused VRAM).
    "szl-fast": [
        ("omen_gpu", "llama3.1:8b"),
        ("box_gpu", "llama3.1:8b"),
        ("nvidia_gpu", "llama3.1:8b"),
        # ultra-fast free grid: Cerebras (≈1M tok/day) first, then Groq instant.
        ("cerebras", "gpt-oss-120b"),
        ("groq", "llama-3.1-8b-instant"),
        ("nvidia_nim", "meta/llama-3.1-8b-instruct"),
        ("openrouter", "meta-llama/llama-3.3-70b-instruct:free"),
    ],
    # coding brain
    "szl-coder": [
        ("box_gpu", "qwen2.5-coder:7b"),
        ("nvidia_gpu", "qwen2.5-coder:7b"),
        ("omen_gpu", "qwen2.5-coder:7b"),
        # free-grid: DeepSeek-R1 (strong on hard/algorithmic code) first, then the
        # existing coder-specialist and 70B fallbacks. New providers skip until armed.
        ("groq", "deepseek-r1-distill-llama-70b"),
        ("openrouter", "qwen/qwen3-next-80b-a3b-instruct:free"),
        ("nvidia_nim", "deepseek-ai/deepseek-coder-6.7b-instruct"),
        ("groq", "llama-3.3-70b-versatile"),
    ],
}

# Embeddings routes. Same sovereign-first doctrine, but for embeddings we bias
# the always-on HOME node (omen_gpu) FIRST so the embeddings/RAG lane lives on
# the always-on desktop and the TRAVELING laptop is not pinned as the embeddings
# dependency. Served through the OpenAI-compatible /v1/embeddings surface in
# app.py. Honest provenance is identical to chat. Free-grid has no honest
# always-on embeddings peer here, so the sovereign nodes are the route; if none
# is armed the call fails loud (no fabricated vector).
EMBED_ROUTES: Dict[str, List[Route]] = {
    "bge-large": [
        ("omen_gpu", "bge-large"),
        ("box_gpu", "bge-large"),
        ("nvidia_gpu", "bge-large"),
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
    # Set ONLY for the opt-in "szl-auto" logical model: the honest, deterministic
    # routing decision (complexity heuristic + chosen real logical model). Absent
    # for every other model so their provenance shape stays byte-identical.
    routing: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "served_by": self.served_by,
            "provider": self.provider,
            "upstream_model": self.upstream_model,
            "base_url": self.base_url,
            "sovereign": self.sovereign,
            "energy_source": self.energy_source,
            "tier": self.tier,
            "attempts": [a.__dict__ for a in self.attempts],
        }
        if self.routing is not None:
            d["routing"] = self.routing
        return d


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
# Upstream connection pool (keep-alive).
#
# Replaces per-call urllib.urlopen (fresh TCP+TLS handshake every request) with
# a bounded pool of keep-alive http.client connections, keyed by (scheme, host,
# port). Pure stdlib so it still runs anywhere with zero install. Transport-only:
# the request shape, headers (incl. the Groq Cloudflare User-Agent quirk), and
# JSON contract are byte-for-byte what _post_chat/_post_embeddings sent before.
# A connection that errors or returns a non-keep-alive response is dropped, not
# reused — so correctness never depends on the pool being warm.
# ---------------------------------------------------------------------------
_POOL_MAX_PER_HOST = 4    # idle keep-alive conns kept per (scheme, host, port)
_POOL_MAX_HOSTS = 16      # distinct hosts tracked before we stop caching new ones


class _ConnectionPool:
    def __init__(self, max_per_host: int = _POOL_MAX_PER_HOST,
                 max_hosts: int = _POOL_MAX_HOSTS):
        self._max_per_host = max_per_host
        self._max_hosts = max_hosts
        self._idle: Dict[Tuple[str, str, int], List[http.client.HTTPConnection]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(scheme: str, host: str, port: int) -> Tuple[str, str, int]:
        return (scheme, host, port)

    def _new_conn(self, scheme: str, host: str, port: int,
                  timeout: float) -> http.client.HTTPConnection:
        if scheme == "https":
            return http.client.HTTPSConnection(host, port, timeout=timeout)
        return http.client.HTTPConnection(host, port, timeout=timeout)

    def _checkout(self, scheme: str, host: str, port: int,
                  timeout: float) -> http.client.HTTPConnection:
        key = self._key(scheme, host, port)
        with self._lock:
            bucket = self._idle.get(key)
            if bucket:
                conn = bucket.pop()
                conn.timeout = timeout
                return conn
        return self._new_conn(scheme, host, port, timeout)

    def _release(self, scheme: str, host: str, port: int,
                 conn: http.client.HTTPConnection) -> None:
        key = self._key(scheme, host, port)
        with self._lock:
            if key not in self._idle and len(self._idle) >= self._max_hosts:
                conn.close()
                return
            bucket = self._idle.setdefault(key, [])
            if len(bucket) >= self._max_per_host:
                conn.close()
                return
            bucket.append(conn)

    def request_json(self, url: str, data: bytes, headers: Dict[str, str],
                     timeout: float) -> Dict[str, Any]:
        """POST `data` to `url` and return the parsed JSON body, reusing a pooled
        keep-alive connection when one is available.

        Raises urllib.error.HTTPError on a non-2xx status so the existing
        failover/honesty handling in chat()/embed() is unchanged."""
        parts = urllib.parse.urlsplit(url)
        scheme = parts.scheme
        host = parts.hostname or ""
        port = parts.port or (443 if scheme == "https" else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query

        conn = self._checkout(scheme, host, port, timeout)
        try:
            try:
                conn.request("POST", path, body=data, headers=headers)
                resp = conn.getresponse()
            except (http.client.HTTPException, OSError):
                # A stale pooled connection can fail at send/recv; retry once on a
                # fresh connection so pool reuse is never observable as an error.
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
                conn = self._new_conn(scheme, host, port, timeout)
                conn.request("POST", path, body=data, headers=headers)
                resp = conn.getresponse()

            status = resp.status
            body = resp.read()
            keep_alive = self._can_keep_alive(resp)
            if status >= 400:
                conn.close()
                raise urllib.error.HTTPError(
                    url, status, resp.reason,
                    {k.lower(): v for k, v in resp.getheaders()},
                    io.BytesIO(body),
                )
            if keep_alive:
                self._release(scheme, host, port, conn)
            else:
                conn.close()
            return json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError:
            raise
        except Exception:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            raise

    @staticmethod
    def _can_keep_alive(resp: http.client.HTTPResponse) -> bool:
        conn_hdr = (resp.getheader("Connection") or "").lower()
        if "close" in conn_hdr:
            return False
        if resp.version == 10 and "keep-alive" not in conn_hdr:
            return False  # HTTP/1.0 defaults to close unless it opts in
        return True


_UPSTREAM_POOL = _ConnectionPool()


# ---------------------------------------------------------------------------
# Transient-error retry with exponential backoff + full jitter.
#
# Distinct from the route-level failover below: the route loop walks DIFFERENT
# providers (sovereign -> free -> paid) and needs no backoff. But a SINGLE
# provider can blip transiently — a rate-limit (429) or an overloaded upstream
# (500/502/503/504), or a dropped connection — where an immediate same-provider
# retry would just hammer it. So before falling through to the next route we
# retry the SAME provider a bounded number of times with exponential backoff and
# FULL jitter (sleep = random(0, base * 2**attempt), capped). Full jitter is the
# AWS-recommended shape: it spreads concurrent clients so they don't retry in
# lockstep and stampede a recovering upstream.
#
# Only transient statuses are retried. A 4xx that is NOT 429 (e.g. 400 bad
# request, 401 bad key, 404 model not found) is a permanent error for this
# provider — retrying can't help, so we fail through to the next route at once.
# Honesty is unchanged: every try (including retried ones) is still ultimately
# surfaced through the same attempt trail; we never fabricate a success.
# ---------------------------------------------------------------------------
_RETRY_MAX_ATTEMPTS = int(os.environ.get("SZL_RETRY_MAX_ATTEMPTS", "3") or 3)  # total tries per provider
_RETRY_BASE_DELAY = float(os.environ.get("SZL_RETRY_BASE_DELAY", "0.25") or 0.25)  # seconds
_RETRY_MAX_DELAY = float(os.environ.get("SZL_RETRY_MAX_DELAY", "4.0") or 4.0)  # seconds cap
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def _is_retryable_http(status: Optional[int]) -> bool:
    return status is not None and status in _RETRYABLE_STATUS


def _backoff_sleep_seconds(attempt: int) -> float:
    """Full-jitter exponential backoff for retry number `attempt` (0-based):
    uniform(0, min(cap, base * 2**attempt)). Pure except for the RNG so the
    schedule is easy to reason about and override via env."""
    ceiling = min(_RETRY_MAX_DELAY, _RETRY_BASE_DELAY * (2 ** attempt))
    return random.uniform(0, ceiling) if ceiling > 0 else 0.0


def _post_with_retry(poster, provider: Provider, payload: Dict[str, Any],
                     timeout: float):
    """Call `poster(provider, payload, timeout)` with same-provider transient
    retry (exponential backoff + full jitter). Re-raises the LAST error once the
    attempt budget is spent or the error is permanent, so the caller's existing
    per-route honesty handling records it exactly as before."""
    last_exc: Exception
    for attempt in range(_RETRY_MAX_ATTEMPTS):
        try:
            return poster(provider, payload, timeout)
        except urllib.error.HTTPError as e:
            last_exc = e
            if not _is_retryable_http(getattr(e, "code", None)):
                raise  # permanent (e.g. 400/401/404) — fail through now
        except (http.client.HTTPException, OSError) as e:
            last_exc = e  # connection-level blip — retryable
        if attempt + 1 >= _RETRY_MAX_ATTEMPTS:
            break
        time.sleep(_backoff_sleep_seconds(attempt))
    raise last_exc


# ---------------------------------------------------------------------------
# Core call
# ---------------------------------------------------------------------------
def _upstream_headers(provider: Provider) -> Dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        # Some upstreams (e.g. Groq behind Cloudflare) reject the default urllib
        # User-Agent with HTTP 1010. Identify ourselves like a normal client.
        "User-Agent": "szl-router/1.0",
        "Accept": "application/json",
    }
    key = provider.api_key()
    if key:
        headers["Authorization"] = "Bearer " + key
    return headers


def _post_chat(provider: Provider, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    url = provider.base_url() + "/chat/completions"
    data = json.dumps(payload).encode("utf-8")
    return _UPSTREAM_POOL.request_json(url, data, _upstream_headers(provider), timeout)


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


# Documented fallback only — real deployments set SZL_RECEIPT_SINK.
_RECEIPT_SINK_FALLBACK = "https://szlholdings-a11oy.hf.space/api/lake/v1"


def _emit_route_receipt(
    *,
    model: str,
    decision: str,
    provenance: Optional["Provenance"],
    attempts: List["Attempt"],
) -> None:
    """Fire-and-forget governance receipt for one routing decision.

    POSTs to <SZL_RECEIPT_SINK>/receipts on a daemon thread with a short
    timeout; every error is swallowed. A sink hiccup never blocks or breaks
    routing. No-op when SZL_RECEIPT_SINK is unset — the fallback URL is only a
    documented default and is never contacted unless the env var is set.
    """
    sink = os.environ.get("SZL_RECEIPT_SINK")
    if not sink:
        return
    prov_dict = provenance.to_dict() if provenance is not None else {}
    served_by = prov_dict.get("served_by")
    canonical = json.dumps(
        {"model": model, "decision": decision, "served_by": served_by},
        sort_keys=True, separators=(",", ":"),
    ).encode()
    rid = hashlib.sha256(canonical + str(time.time_ns()).encode()).hexdigest()
    payload = {
        "id": rid,
        "ts": time.time(),
        "organ": "szl-router",
        "decision": decision,  # served | all-routes-failed
        "governance": {
            # The router computes no Λ score — honest null, never fabricated.
            "lambda": None,
            "gates": {
                "model": model,
                "served_by": served_by,
                "tier": prov_dict.get("tier"),
                "sovereign": prov_dict.get("sovereign"),
                "energy_source": prov_dict.get("energy_source"),
                "attempts": len(attempts),
            },
        },
        # No joules meter in the router — honest UNAVAILABLE, never fabricated.
        "energy": {"label": "UNAVAILABLE", "joules": None},
    }

    def _send() -> None:
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                sink.rstrip("/") + "/receipts",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=2.0).close()
        except Exception:
            pass  # fire-and-forget: never raise, never block routing

    try:
        threading.Thread(target=_send, daemon=True).start()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# szl-auto: opt-in complexity-aware, sovereign-first smart routing.
#
# "szl-auto" is NOT a route of its own — chat() intercepts it, scores the prompt
# with a deterministic, no-LLM heuristic, and dispatches to the cheapest CAPABLE
# real logical model SOVEREIGN-FIRST: simple -> szl-fast, complex -> szl-large,
# code -> szl-coder. The decision (score, signals, chosen model) is recorded
# HONESTLY in x_szl_provenance.routing and inside the signed receipt. It is a
# routing ESTIMATE, never a quality guarantee, and it costs no upstream call.
# ---------------------------------------------------------------------------
AUTO_MODEL = "szl-auto"
AUTO_SCORER_VERSION = "heuristic-v1"

_CODE_MARKERS = (
    "```", "def ", "return ", "import ", "function", "class ", "();", "});",
    "const ", "let ", " var ", "select ", "#include", "public static",
    "console.log", "print(", "</", "/>", "traceback", "stack trace",
    "npm ", "pip install", "regex",
)
_REASONING_MARKERS = (
    "why", "explain", "compare", "analyze", "analyse", "step by step",
    "prove", "derive", "trade-off", "tradeoff", "design", "architect",
    "evaluate", "reason", "implications", "pros and cons", "in detail",
    "comprehensive", "strategy", "optimize", "refactor", "debug",
)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _coerce_text(content: Any) -> str:
    """Flatten an OpenAI message 'content' (str, list-of-parts, or other) to
    plain text for lexical scoring. Never raises; unknown shapes -> str()."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                parts.append(str(p.get("text") or ""))
            else:
                parts.append(str(p))
        return " ".join(parts)
    return str(content)


def score_complexity(messages: List[Dict[str, Any]]) -> Tuple[float, List[str], str]:
    """Deterministic, no-LLM heuristic ESTIMATE of prompt complexity.

    Returns (score in [0,1], human-readable signals, chosen logical model). Pure
    and side-effect free, so the same prompt always routes the same way and a
    receipt can be reproduced. This is a routing choice, never a quality claim,
    and makes NO upstream call.
    """
    signals: List[str] = []
    # Fail-safe: an "szl-auto" request whose `messages` is malformed (a JSON
    # object instead of an array, a bare string, null, or any other non-list
    # shape) must still resolve to a route — never crash the smart-router. A
    # non-list `messages` previously reached `msgs[-1]` and raised (e.g. a dict
    # payload -> KeyError), turning a bad request into an unhandled 500. Normalize
    # any non-list to an empty list so the scorer is TOTAL: a malformed payload
    # degrades to the cheap sovereign-first default (szl-fast) and the real
    # upstream call still surfaces the bad body honestly in the attempt trail.
    msgs = messages if isinstance(messages, list) else []
    user_msgs = [m for m in msgs if isinstance(m, dict) and m.get("role") == "user"]
    last = user_msgs[-1] if user_msgs else (msgs[-1] if msgs else {})
    text = _coerce_text(last.get("content") if isinstance(last, dict) else last)
    all_text = " ".join(_coerce_text(m.get("content")) for m in msgs
                        if isinstance(m, dict))
    lower = text.lower()
    all_lower = all_text.lower()
    n_chars = len(text)
    n_msgs = len(msgs)

    # Code detection (routes to szl-coder regardless of length).
    code_hits = [mk for mk in _CODE_MARKERS if mk in all_lower]
    is_code = ("```" in all_text) or (len(code_hits) >= 2)

    # Complexity score, baseline 0.30.
    score = 0.30
    if n_chars > 1200:
        score += 0.35
        signals.append("very-long-prompt")
    elif n_chars > 400:
        score += 0.20
        signals.append("long-prompt")
    elif n_chars < 120:
        score -= 0.12
        signals.append("short-prompt")
    if n_msgs >= 6:
        score += 0.15
        signals.append("deep-conversation")
    reasoning = [w for w in _REASONING_MARKERS if w in lower]
    if reasoning:
        score += min(0.30, 0.10 * len(reasoning))
        signals.append("reasoning:" + ",".join(reasoning[:3]))
    if text.count("?") >= 3:
        score += 0.10
        signals.append("multi-question")
    score = max(0.0, min(1.0, round(score, 3)))

    large_threshold = _env_float("SZL_AUTO_LARGE_THRESHOLD", 0.50)
    if is_code:
        chosen = "szl-coder"
        signals.insert(0, "code:" + ",".join(code_hits[:3]) if code_hits else "code-fence")
    elif score >= large_threshold:
        chosen = "szl-large"
    else:
        chosen = "szl-fast"
    return score, signals, chosen


def _auto_routing_block(score: float, signals: List[str], chosen: str) -> Dict[str, Any]:
    return {
        "router": AUTO_MODEL,
        "method": f"{AUTO_SCORER_VERSION}: deterministic lexical scorer, no LLM call",
        "complexity_score": score,
        "signals": signals,
        "chosen_logical": chosen,
        "note": ("heuristic ESTIMATE of prompt complexity to pick a "
                 "sovereign-first route — a routing choice, not a quality "
                 "guarantee"),
    }


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
    `x_szl_provenance` block. Raises RouterError if every route fails.

    The opt-in "szl-auto" model is intercepted here: its prompt is scored by a
    deterministic no-LLM heuristic and dispatched to the cheapest capable real
    logical model (sovereign-first). `model` stays "szl-auto" on the receipt so
    the caller sees WHAT they asked for; the chosen real model and served
    provider are recorded honestly in provenance.routing + served_by."""
    routing_block: Optional[Dict[str, Any]] = None
    route_model = model
    if model == AUTO_MODEL:
        _score, _signals, _chosen = score_complexity(messages)
        routing_block = _auto_routing_block(_score, _signals, _chosen)
        route_model = _chosen
    routes = resolve_routes(route_model)
    prov = Provenance(routing=routing_block)
    attempts: List[Attempt] = []

    for provider_name, upstream_model in routes:
        provider = PROVIDERS.get(provider_name)
        if provider is None or not provider.available():
            attempts.append(Attempt(provider_name, upstream_model, ok=False,
                                    error="provider unavailable (no key/url)"))
            continue

        # SPEND GUARD (SZL Sovereign Ops): a PAID tier may never spend past the
        # hard USD cap or while the kill-switch is engaged. Sovereign/free tiers
        # cost nothing and are never gated. A blocked paid route falls through to
        # the next (cheaper) route honestly, recorded in the attempt trail.
        if _tier_of(provider) == "paid-grid":
            _sg_ok, _sg_why = spend_guard.allow()
            if not _sg_ok:
                attempts.append(Attempt(provider_name, upstream_model, ok=False,
                                        error="spend-cap blocked paid tier: " + _sg_why))
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
            result = _post_with_retry(_post_chat, provider, payload, timeout)
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
            # SPEND GUARD: record estimated USD for a served PAID call so the
            # append-only ledger stays honest (free/sovereign record nothing).
            if _tier_of(provider) == "paid-grid":
                try:
                    spend_guard.record(spend_guard.estimate_usd(result),
                                       source=prov.served_by or provider_name,
                                       meta={"model": model})
                except Exception:
                    pass
            _emit_route_receipt(model=model, decision="served",
                                provenance=prov, attempts=attempts)
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

    _emit_route_receipt(model=model, decision="all-routes-failed",
                        provenance=None, attempts=attempts)
    raise RouterError(f"all routes failed for model '{model}'", attempts)


def _post_embeddings(provider: Provider, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    url = provider.base_url() + "/embeddings"
    data = json.dumps(payload).encode("utf-8")
    return _UPSTREAM_POOL.request_json(url, data, _upstream_headers(provider), timeout)


def resolve_embed_routes(model: str) -> List[Route]:
    """Map a requested embeddings model to its ordered routes (HOME-node first).

    Accepts our logical embed name (bge-large) OR a raw 'provider:model'. Unknown
    logical names fall back to the bge-large route so callers stay honest."""
    if model in EMBED_ROUTES:
        return EMBED_ROUTES[model]
    if ":" in model:
        prov, _, up = model.partition(":")
        if prov in PROVIDERS:
            return [(prov, up)]
    return EMBED_ROUTES["bge-large"]


# ---------------------------------------------------------------------------
# Exact-hash embeddings cache.
#
# Embeddings are a pure function of (model, input, extra) — identical inputs
# yield identical vectors — and are highly repeat-prone (RAG re-indexing the
# same docs). So an EXACT-hash cache is honesty-safe: a hit returns the SAME
# bytes the upstream returned, never a fabricated or approximate vector. Reuses
# the proven _HARVEST_CACHE TTL idea (in-process, time-bounded) plus a size cap.
#
# Chat is deliberately NOT cached: it is correctness-sensitive (temperature,
# tools, non-determinism), so caching it could silently change behavior.
#
# On a hit, provenance.served_by is suffixed with the honest ":cache" marker and
# the response carries x_szl_cache so callers can SEE the answer came from cache.
# The cached vectors and the upstream tier/sovereign labels are unchanged.
# ---------------------------------------------------------------------------
_EMBED_CACHE_TTL = float(os.environ.get("SZL_EMBED_CACHE_TTL", "300") or 300)  # seconds
_EMBED_CACHE_MAX = int(os.environ.get("SZL_EMBED_CACHE_MAX", "1024") or 1024)  # entries
_EMBED_CACHE: "Dict[str, Tuple[float, Dict[str, Any]]]" = {}
_EMBED_CACHE_LOCK = threading.Lock()


def _embed_cache_key(model: str, input_: Any, extra: Optional[Dict[str, Any]]) -> str:
    """Deterministic key over the full request shape. sort_keys makes dict order
    irrelevant; default=str keeps non-JSON-native inputs hashable rather than
    crashing the hot path."""
    payload = {"model": model, "input": input_, "extra": extra or {}}
    blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _embed_cache_get(key: str) -> Optional[Dict[str, Any]]:
    now = time.time()
    with _EMBED_CACHE_LOCK:
        hit = _EMBED_CACHE.get(key)
        if hit is None:
            return None
        ts, value = hit
        if (now - ts) >= _EMBED_CACHE_TTL:
            _EMBED_CACHE.pop(key, None)
            return None
        return value


def _embed_cache_put(key: str, value: Dict[str, Any]) -> None:
    now = time.time()
    with _EMBED_CACHE_LOCK:
        if len(_EMBED_CACHE) >= _EMBED_CACHE_MAX and key not in _EMBED_CACHE:
            # Simple bound: drop the oldest entry. Cheap and adequate for an
            # in-process exact-hash cache; not an LRU on purpose (no per-get cost).
            oldest = min(_EMBED_CACHE, key=lambda k: _EMBED_CACHE[k][0])
            _EMBED_CACHE.pop(oldest, None)
        _EMBED_CACHE[key] = (now, value)


def embed_cache_clear() -> None:
    """Drop all cached embeddings (test/ops helper)."""
    with _EMBED_CACHE_LOCK:
        _EMBED_CACHE.clear()


def _as_cache_hit(cached: Dict[str, Any]) -> Dict[str, Any]:
    """Return a cached embeddings response, honestly marked as cache-served.

    Deep-copies so callers can't mutate the stored entry. The vectors (`data`)
    are byte-identical to what the upstream returned. Provenance is preserved
    exactly (same provider/tier/sovereign/energy_source — we don't relabel where
    the vector was actually computed) except served_by gains an honest ":cache"
    suffix, and an `x_szl_cache` block makes the cache hit explicit."""
    out = json.loads(json.dumps(cached))  # cheap deep copy of JSON-shaped data
    prov = out.get("x_szl_provenance")
    origin = prov.get("served_by") if isinstance(prov, dict) else None
    if isinstance(prov, dict):
        if origin and not str(origin).endswith(":cache"):
            prov["served_by"] = f"{origin}:cache"
    out["x_szl_cache"] = {
        "served_by": "cache",
        "hit": True,
        "origin_served_by": origin,
        "note": "exact-hash embeddings cache hit; vectors byte-identical to the "
                "upstream result; no recompute, no fabrication.",
    }
    return out


def embed(
    model: str,
    input_: Any,
    *,
    timeout: float = 60.0,
    extra: Optional[Dict[str, Any]] = None,
    use_cache: bool = True,
) -> Dict[str, Any]:
    """Run an embeddings call through the HOME-node-first sovereign chain.

    Mirrors chat(): OpenAI-shaped /v1/embeddings response with an added
    `x_szl_provenance` block (identical honesty contract — served_by, sovereign
    true ONLY for owned metal, tier, full attempts trail). Each sovereign node is
    a SEPARATE worker; this is sequential failover, never fused VRAM. Raises
    RouterError if every route fails (no fabricated vector).

    An exact-hash cache (TTL + size cap) serves byte-identical vectors for a
    repeated (model, input, extra) request and marks the answer honestly as
    served_by ...:cache. Set use_cache=False to force a fresh upstream call."""
    routes = resolve_embed_routes(model)

    cache_key = _embed_cache_key(model, input_, extra) if use_cache else None
    if cache_key is not None:
        cached = _embed_cache_get(cache_key)
        if cached is not None:
            return _as_cache_hit(cached)

    prov = Provenance()
    attempts: List[Attempt] = []

    for provider_name, upstream_model in routes:
        provider = PROVIDERS.get(provider_name)
        if provider is None or not provider.available():
            attempts.append(Attempt(provider_name, upstream_model, ok=False,
                                    error="provider unavailable (no key/url)"))
            continue

        payload: Dict[str, Any] = {"model": upstream_model, "input": input_}
        if extra:
            payload.update(extra)

        t0 = time.time()
        try:
            result = _post_with_retry(_post_embeddings, provider, payload, timeout)
            dt = int((time.time() - t0) * 1000)
            if "data" not in result:
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
            if cache_key is not None:
                _embed_cache_put(cache_key, result)
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

    raise RouterError(f"all embed routes failed for model '{model}'", attempts)


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
        "embed_models": {k: v for k, v in EMBED_ROUTES.items()},
        "default_model": DEFAULT_MODEL,
    }


# energy_source values we are allowed to claim, honestly. "self-hosted" and
# "grid" are the only TRUE ones today (our own metal still runs on grid power;
# third-party clouds are grid). The rest are the Sovereign-Resilience / Stranded-
# Energy roadmap targets — only ever set when verifiably true (no greenwashing).
HONEST_ENERGY_SOURCES = ["self-hosted", "grid"]
ROADMAP_ENERGY_SOURCES = ["solar", "curtailed-wind", "hydro", "flare-mitigated", "biogas"]


# ---------------------------------------------------------------------------
# Wasted-energy harvest signal (R-HARVEST-FABRIC).
#
# Real, no-key, public grid feeds that tell us when the grid is WASTING power —
# i.e. when running our own GPU is effectively free because the grid is paying
# to offload load (negative wholesale price) or renewables exceed demand
# (curtailment). We never invent numbers: each feed is fetched independently and
# tolerantly; a feed that is down is reported "unreachable", not faked. This is
# REAL GRID DATA under the HONEST "grid" source — it NEVER flips the sovereign
# label and joules stay SAMPLE until an on-box hardware meter (NVML) feeds them.
# ---------------------------------------------------------------------------
_HARVEST_FEEDS = {
    "wholesale_price": "https://api.awattar.de/v1/marketdata",          # DE wholesale, EUR/MWh
    "renewable_share": "https://api.energy-charts.info/ren_share?country=de",
    "carbon_intensity": "https://api.carbonintensity.org.uk/intensity",  # UK gCO2/kWh + index
}
_HARVEST_TTL = 300.0  # seconds; don't hammer the public feeds
_HARVEST_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}


def _get_json(url: str, timeout: float) -> Any:
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", "szl-router/1.0 (+harvest)")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_awattar(timeout: float) -> Dict[str, Any]:
    try:
        d = _get_json(_HARVEST_FEEDS["wholesale_price"], timeout)
        rows = d.get("data") or []
        now_ms = time.time() * 1000
        price_now: Optional[float] = None
        nxt: List[float] = []
        for r in rows:
            st, en, mp = r.get("start_timestamp"), r.get("end_timestamp"), r.get("marketprice")
            if st is None or en is None or mp is None:
                continue
            if st <= now_ms < en:
                price_now = mp
            elif st > now_ms:
                nxt.append(mp)
        return {
            "status": "live", "market": "DE", "unit": "EUR/MWh",
            "price_now": price_now,
            "next_min": (min(nxt) if nxt else None),
            "next_max": (max(nxt) if nxt else None),
            "next_negative_windows": sum(1 for p in nxt if p < 0),
            "source": "api.awattar.de",
        }
    except Exception as e:  # noqa: BLE001 - honest unreachable, never faked
        return {"status": "unreachable", "error": f"{type(e).__name__}: {e}"[:120],
                "source": "api.awattar.de"}


def _fetch_ren_share_de(timeout: float) -> Dict[str, Any]:
    try:
        d = _get_json(_HARVEST_FEEDS["renewable_share"], timeout)
        series = d[0] if isinstance(d, list) and d else {}
        data = series.get("data") or []
        vals = [v for v in data if isinstance(v, (int, float))]
        return {"status": "live", "country": "DE",
                "renewable_share_pct": (vals[-1] if vals else None),
                "source": "api.energy-charts.info"}
    except Exception as e:  # noqa: BLE001
        return {"status": "unreachable", "error": f"{type(e).__name__}: {e}"[:120],
                "source": "api.energy-charts.info"}


def _fetch_uk_carbon(timeout: float) -> Dict[str, Any]:
    try:
        d = _get_json(_HARVEST_FEEDS["carbon_intensity"], timeout)
        intensity = ((d.get("data") or [{}])[0] or {}).get("intensity") or {}
        return {"status": "live", "region": "UK",
                "gco2_per_kwh": intensity.get("actual") if intensity.get("actual") is not None
                else intensity.get("forecast"),
                "index": intensity.get("index"),
                "source": "api.carbonintensity.org.uk"}
    except Exception as e:  # noqa: BLE001
        return {"status": "unreachable", "error": f"{type(e).__name__}: {e}"[:120],
                "source": "api.carbonintensity.org.uk"}


# Posture thresholds. Kept as a pure function so the classification is
# deterministic and unit-testable WITHOUT any network.
_CHEAP_EUR_MWH = 30.0
_EXPENSIVE_EUR_MWH = 100.0
_CURTAILMENT_REN_PCT = 100.0  # renewables meeting/exceeding load


def _classify_harvest(
    price_now: Optional[float],
    next_min: Optional[float],
    ren_share_pct: Optional[float],
) -> Tuple[str, bool, bool]:
    """-> (grid_price_posture, wasted_energy_available, next_window_negative).

    Pure. posture in
    {negative-price, curtailed-renewable, cheap, normal, expensive, unknown}."""
    window_ahead = next_min is not None and next_min < 0
    if price_now is not None and price_now < 0:
        return "negative-price", True, window_ahead       # grid PAYING to offload
    if ren_share_pct is not None and ren_share_pct >= _CURTAILMENT_REN_PCT:
        return "curtailed-renewable", True, window_ahead  # clean power > demand
    if price_now is not None and price_now < _CHEAP_EUR_MWH:
        return "cheap", False, window_ahead
    if price_now is not None and price_now > _EXPENSIVE_EUR_MWH:
        return "expensive", False, window_ahead
    if price_now is not None or ren_share_pct is not None:
        return "normal", False, window_ahead
    return "unknown", False, window_ahead


def harvest_status(allow_network: bool = True, timeout: float = 8.0,
                   force: bool = False) -> Dict[str, Any]:
    """Live wasted-energy harvest posture from real public grid feeds.

    Cached for _HARVEST_TTL so /fabric stays cheap. With allow_network=False it
    serves the last cache (or an honest 'not-probed' if none). NEVER fabricates:
    a down feed is reported 'unreachable' and simply doesn't drive the posture."""
    now = time.time()
    cached = _HARVEST_CACHE.get("data")
    if cached is not None and not force and (now - float(_HARVEST_CACHE["ts"])) < _HARVEST_TTL:
        out = dict(cached); out["cached"] = True; return out
    if not allow_network:
        if cached is not None:
            out = dict(cached); out["cached"] = True; return out
        return {
            "status": "not-probed", "grid_price_posture": "unknown",
            "wasted_energy_available": False, "next_window_negative": False,
            "energy_source": "free-public-grid-feeds", "joules_label": "sample",
            "sovereign": False, "note": "offline: no network probe performed",
        }

    awattar = _fetch_awattar(timeout)
    ren = _fetch_ren_share_de(timeout)
    carbon = _fetch_uk_carbon(timeout)
    price_now = awattar.get("price_now") if awattar.get("status") == "live" else None
    next_min = awattar.get("next_min") if awattar.get("status") == "live" else None
    ren_pct = ren.get("renewable_share_pct") if ren.get("status") == "live" else None
    posture, wasted, window_ahead = _classify_harvest(price_now, next_min, ren_pct)
    any_live = any(s.get("status") == "live" for s in (awattar, ren, carbon))

    data = {
        "status": "live" if any_live else "unreachable",
        "grid_price_posture": posture,
        "wasted_energy_available": wasted,
        "next_window_negative": window_ahead,
        "energy_source": "free-public-grid-feeds",
        "joules_label": "sample",   # no on-router meter — MEASURED only on-box via NVML
        "sovereign": False,         # grid data NEVER flips the sovereign label
        "signals": {"wholesale_price": awattar, "renewable_share": ren,
                    "carbon_intensity": carbon},
        "doctrine": ("we soak ALREADY-WASTED grid energy; no free-energy; joules SAMPLE "
                     "until an on-box meter; this grid signal NEVER sets sovereign:true."),
        "cached": False,
    }
    _HARVEST_CACHE["ts"] = now
    _HARVEST_CACHE["data"] = data
    return dict(data)


def should_soak_wasted_energy(allow_network: bool = True) -> bool:
    """PROACTIVE/batch admission gate: True only when the grid is effectively
    paying us to absorb load (negative wholesale price) or renewables exceed
    demand (curtailment). Reactive/user turns NEVER consult this — they always
    serve. Mirrors the platform energy_gate_adapter contract."""
    try:
        return bool(harvest_status(allow_network=allow_network).get("wasted_energy_available"))
    except Exception:  # noqa: BLE001 - fail closed: no soak if we can't read the grid
        return False


def fabric_status(include_harvest: bool = True, allow_network: bool = True) -> Dict[str, Any]:
    """Energy/sovereignty posture of the whole fabric — honest, live.

    Maps the live provider registry onto the Sovereign-Resilience tier ladder
    (sovereign own-metal first, then free grid faucets, then paid grid) and
    reports a single posture: green = a sovereign node is up AND has a fallback;
    yellow = degraded (only one route, or no sovereign node up); red = nothing
    armed. Never claims sovereign/clean-energy that isn't literally true.

    When include_harvest, overlays the live wasted-energy grid-price posture
    (R-HARVEST-FABRIC) under the HONEST grid source. If a real wasted-energy
    window is open AND a sovereign node is up, surfaces a distinct, honest
    HARVESTING display state (real wasted grid power soaked on our own metal —
    not greenwash). The harvest overlay NEVER changes the sovereign label."""
    sovereign, free_grid, paid_grid = [], [], []
    for name, p in PROVIDERS.items():
        rec = {
            "provider": name,
            "armed": p.available(),
            "sovereign": p.sovereign,
            "energy_source": p.energy_source,
            "note": p.note,
        }
        tier = _tier_of(p)
        (sovereign if tier == "sovereign"
         else paid_grid if tier == "paid-grid"
         else free_grid).append(rec)

    sov_armed = [r for r in sovereign if r["armed"]]
    grid_armed = [r for r in (free_grid + paid_grid) if r["armed"]]
    total_armed = len(sov_armed) + len(grid_armed)
    if sov_armed and total_armed >= 2:
        posture = "green"      # sovereign up + at least one fallback
    elif total_armed >= 1:
        posture = "yellow"     # degraded: no sovereign, or no redundancy
    else:
        posture = "red"        # nothing armed

    harvest = harvest_status(allow_network=allow_network) if include_harvest else None
    wasted = bool(harvest and harvest.get("wasted_energy_available"))
    harvesting = wasted and bool(sov_armed)   # real wasted power + our own metal up
    energy_window = (harvest.get("grid_price_posture") if harvest else "not-probed")
    if harvesting:
        display_state = "HARVESTING"
        harvest_hint = ("real wasted-energy window open AND sovereign GPU up: bias "
                        "proactive/batch routing to own metal now (grid is paying to offload).")
    elif wasted:
        display_state = posture
        harvest_hint = ("wasted-energy window open but no sovereign node up: bring the GPU "
                        "online to soak it; do NOT label grid as sovereign.")
    else:
        display_state = posture
        harvest_hint = "no wasted-energy window; normal sovereign-first routing."

    out: Dict[str, Any] = {
        "posture": posture,            # resilience color (route availability)
        "display_state": display_state,  # HARVESTING overlay when truly harvesting
        "harvesting": harvesting,
        "energy_window": energy_window,  # negative-price | curtailed-renewable | cheap | normal | expensive | unknown | not-probed
        "wasted_energy_available": wasted,
        "prefer_sovereign_for_batch": harvesting,
        "harvest_hint": harvest_hint,
        "sovereign_up": bool(sov_armed),
        "routes_armed": total_armed,
        "ladder": {
            "tier_0_allodial_solar": {
                "status": "roadmap",
                "what": "own weights + own solar + own metal = unkillable, sovereign:true",
                "energy_source": "solar (not yet — claim only when literally true)",
            },
            "tier_1_sovereign_gpu": {
                "status": "live" if sov_armed else "armed-but-down" if sovereign else "absent",
                "providers": sovereign,
                "what": "our own GPU node(s) over Tailscale; sovereign:true, grid power today",
            },
            "tier_2_free_grid_faucets": {
                "status": "live" if [r for r in free_grid if r["armed"]] else "configured",
                "providers": free_grid,
                "what": "free open-weight clouds (Groq, NIM, GLM-Flash, SiliconFlow); sovereign:false",
            },
            "tier_3_paid_grid": {
                "providers": paid_grid,
                "what": "paid last-resort (Kimi); sovereign:false",
            },
        },
        "honest_energy_sources": HONEST_ENERGY_SOURCES,
        "roadmap_energy_sources": ROADMAP_ENERGY_SOURCES,
        "doctrine": "sovereign:true ONLY on own metal; energy_source claims must be real; "
                    "free faucets are honest sovereign:false; harvest is real grid data "
                    "(never sovereign); joules SAMPLE until an on-box meter; no half-state.",
    }
    if harvest is not None:
        out["harvest"] = harvest
    return out
