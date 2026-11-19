"""
SZL Router — our own unified, OpenAI-compatible LLM router.

Design goals (learned from the leaders — LiteLLM, OpenRouter, RouteLLM — then
made our own, leaner):
  * ONE OpenAI-compatible surface in front of many upstreams.
  * Sovereign-first routing: try our own metal (GPU on our box) before any
    third-party grid tier, before any paid tier.
  * Honest provenance on every answer: served_by, base_url, sovereign,
    energy_source, tier, attempts. Grid candidates are never sovereign or
    presumed free from provider identity alone.
  * Keys ONLY from the environment. Nothing secret is ever written to disk.
  * No half-state: a logical model either resolves to a working upstream or the
    call fails loud with the full attempt trail.

The call layer here is pure stdlib (urllib) on purpose, so it runs anywhere with
zero install and is trivially testable. The HTTP server wrapper lives in app.py.
"""

from __future__ import annotations

import base64
import binascii
import contextvars
import hashlib
import hmac
import http.client
import io
import json
import math
import os
import random
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    from . import spend_guard  # Legacy offline paid estimate and advisory ledger.
except ImportError:  # test harness imports `core` top-level (sys.path=szl_router)
    import spend_guard  # type: ignore

try:
    from .request_budget import BudgetStop, RequestBudget
except ImportError:  # test harness imports `core` top-level (sys.path=szl_router)
    from request_budget import BudgetStop, RequestBudget  # type: ignore


# ---------------------------------------------------------------------------
# Provider registry. base_url is the OpenAI-compatible root (ends before /chat).
# sovereign is TRUE only for hardware we own/control. energy_source is honest:
# "grid" for t
hird-party clouds; "self-hosted" for our own metal (still grid
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
    enable_env: str = ""      # optional operator opt-in; default disabled

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
        if self.enable_env and os.environ.get(self.enable_env, "").lower() not in {"1", "true", "yes", "on"}:
            return False
        if not self.base_url():
            return False
        if self.key_env:
            return self.api_key() is not None
        return True


PROVIDERS: Dict[str, Provider] = {
    # Explicit opt-in only: local_ollama:<installed-model>. No key, remote URL,
    # model download, or cloud fallback; existing logical route
s are unchanged.
    "local_ollama": Provider(
        name="local_ollama", base_url_env="",
        base_url_default="http://127.0.0.1:11434/v1", key_env="",
        sovereign=True, energy_source="self-hosted",
        note="Loopback Ollama; explicit local model, energy unmeasured.",
        enable_env="SZL_LOCAL_OLLAMA_ENABLE",
    ),
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
    # --- third-party grid candidates (not price/quota qualified) -------------
    "groq": Provider(
        name="groq",
        base_url_env="GROQ_BASE_URL",
        base_url_default="https://api.groq.com/openai/v1",
        key_env="GROQ_API_KEY",
        sovereign=False,
        energy_source="grid",
        note="Cloud inference candid
ate; model pricing and quota unverified.",
    ),
    "nvidia_nim": Provider(
        name="nvidia_nim",
        base_url_env="NVIDIA_NIM_BASE_URL",
        base_url_default="https://integrate.api.nvidia.com/v1",
        key_env="NVIDIA_NIM_API_KEY",
        sovereign=False,
        energy_source="grid",
        note="NVIDIA-hosted NIM catalog; model pricing and quota unverified.",
    ),
    "zhipu": Provider(
        name="zhipu",
        base_url_env="ZHIPU_BASE_URL",
        base_url_default="https://open.bigmodel.cn/api/paas/v4",
        key_env="ZHIPU_API_KEY",
        sovereign=False,
        energy_source="grid",
        note="GLM API candidate; model pricing, quota, and terms unverified.",
    ),
    "siliconflow": Provider(
        name="siliconflow",
        base_url_env="SILICONFLOW_BASE_URL",
        base_url_default="https://api.siliconflow.com/v1",
        key_env="SILICONFLOW_API_KEY",
        sovereign=False,
        energy_source="grid",
        note="Inference API candidate; model pricing, quota, and terms unverified.",
    ),
    "cerebras": Provider(
        name="cerebras",
        base_url_env="CEREBRAS_BASE_URL",
        base_url_default="https://api.cerebras.ai/v1",
        key_env="CEREBRAS_API_KEY",
        sovereign=False,
        energy_source="grid",
        note="Cloud inference candidate; model pricing and quota unverified.",
    ),
    "openrouter": Provider(
        name="openrouter",
        base_url_env="OPENROUTER_BASE_URL",
        base_url_default="https://openrouter.ai/api/v1",
        key_env="OPENROUTER_API_KEY",
        sovereign=False,
        energy_source="grid",
        note="Aggregator candidate; :free suffix does not qualify pricing or quota.",
    ),
    "google": Provider(
        name="google",
        base_url_env="GEMINI_BASE_URL",
        base_url_default="https://generativelanguage.googleapis.com/v1beta/openai",
        key_env="GEMINI_API_KEY",
        sovereign=False,
        energy_source="grid",
        not
e="Gemini API candidate; pricing, quota, terms, and data use unverified.",
    ),
    # --- qualified paid grid (owner-qualified 2026-11-16) --------------------
    "openai": Provider(
        name="openai",
        base_url_env="OPENAI_BASE_URL",
        base_url_default="https://api.openai.com/v1",
        key_env="OPENAI_API_KEY",
        sovereign=False,
        energy_source="grid",
        note="Owner-qualified paid route; served only under a strict pre-call USD reservation (kill switch, max_tokens ceiling, spend cap).",
    ),
    # --- paid grid candidate (blocked until qualified) -----------------------
    "moonshot": Provider(
        name="moonshot",
        base_url_env="MOONSHOT_BASE_URL",
        base_url_default="https://api.moonshot.ai/v1",
        key_env="KIMI_API_KEY",
        sovereign=False,
        energy_source="grid",
        note="Kimi paid cloud candidate; routing blocked until pricing and strict pre-call spend reservation are qualified.",
    ),
}


# ---------------------------------------------------------------------------
# Logical model routes. Each logical name resolves to an ORDERED fallback list
# of (provider, upstream_model). Order encodes the doctrine: sovereign first,
# then grid candidates, then paid. Cloud candidates are skipped even if a key
# is present; key availability does not establish price, quota, or a spend cap.
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
        # Grid can
didates remain listed for later model-specific qualification.
        # They are skipped until pricing and quota are verified.
        ("groq", "deepseek-r1-distill-llama-70b"),
        ("nvidia_nim", "deepseek-ai/deepseek-r1"),
        ("cerebras", "gpt-oss-120b"),
        ("openrouter", "qwen/qwen3-next-80b-a3b-instruct:free"),
        ("google", "gemini-2.5-flash"),
        # 2026-11-19 frontier-board candidates (BenchLeader open-weights
        # board: Kimi K3.1 Pro; Z.AI GLM-5.3). Same tier as the rows above:
        # unqualified grid. Skipped until per-model pricing, quota, and a
        # strict pre-call spend reservation are owner-qualified. Upstream
        # model IDs re-verified at qualification time. Listed presence never
        # enables a route; no sovereign or armed route changed.
        ("zhipu", "glm-5.3"),
        ("moonshot", "kimi-k3.1-pro"),
        # Further grid candidates, then the blocked paid candidate.
        ("groq", "llama-3.3-70b-versatile"),
        ("nvidia_nim", "meta/llama-3.3-70b-instruct"),
        ("moonshot", "kimi-k2.5"),
        # Owner-qualified paid fallback (2026-11-16). Deliberately LAST in the
        # chain: sovereign first, then grid, and paid only as the last resort,
        # and then only after a strict pre-call USD reservation passes.
        ("openai", "gpt-5.4"),
    ],
    # low-latency small brain. OFFLOAD doctrine: prefer the always-on HOME node
    # (omen_gpu) FIRST for small/fast jobs so the TRAVELING Blackwell laptop
    # (box_gpu) is not the sole worker. If OMEN is not armed/reachable this
    # falls through honestly to the laptop, then grid candidates. Each is a
    # separate sovereign worker (sequential failover, never fused VRAM).
    "szl-fast": [
        ("omen_gpu", "llama3.1:8b"),
        ("box_gpu", "llama3.1:8b"),
        ("nvidia_gpu", "llama3.1:8b"),
        # Grid candidates retained for later model-specific qualification.
        ("cerebras", "gpt-oss-120b"),
        ("groq", "llama-3.1-8b-instant"),
        ("nvidia_nim", "meta/llama-3.1-8b-instruct"),
        ("openrouter", "meta-llama/llama-3.3-70b-instruct:free"),
        # 2026-11-19 frontier-board candidate (cheap tier: GLM 5.3 Flash,
        # $0.238/M blended per BenchLeader 2026-11-19). Unqualified grid,
        # skipped until owner qualification. No armed route or ordering change.
        ("zhipu", "glm-5.3-flash"),
        # Owner-qualified paid fallback (2026-11-16), strictly last resort.
        ("openai", "gpt-5-mini"),
    ],
    # coding brain
    "szl-coder": [
        ("box_gpu", "qwen2.5-coder:7b"),
        ("nvidia_gpu", "qwen2.5-coder:7b"),
        ("omen_gpu", "qwen2.5-coder:7b"),
        # Grid candidates retained for later model-specific qualification.
        ("gro
q", "deepseek-r1-distill-llama-70b"),
        ("openrouter", "qwen/qwen3-next-80b-a3b-instruct:free"),
        ("nvidia_nim", "deepseek-ai/deepseek-coder-6.7b-instruct"),
        ("groq", "llama-3.3-70b-versatile"),
        # Owner-qualified paid fallback (2026-11-16), strictly last resort.
        ("openai", "gpt-5.4"),
    ],
}

# Embeddings routes. Same sovereign-first doctrine, but for embeddings we bias
# the always-on HOME node (omen_gpu) FIRST so the embeddings/RAG lane lives on
# the always-on desktop and the TRAVELING laptop is not pinned as the embeddings
# dependency. Served through the OpenAI-compatible /v1/embeddings surface in
# app.py. Honest provenance is identical to chat. There is no qualified cloud
# embeddings route here, so the sovereign nodes are the route; if none
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
    tier: Optional[str] = None             # sovereign | unqualified-grid | paid-grid | qualified-paid
    attempts: List[Attempt] = field(default_factory=list)
    # Set ONLY for the opt-in "szl-auto" logical model: the honest, deterministic
    # routing decision (complexity heuristic + cho
sen real logical model). Absent
    # for every other model so their provenance shape stays byte-identical.
    routing: Optional[Dict[str, Any]] = None
    # Honest per-call USD cost block for the SERVED route (see _cost_detail):
    # paid tier -> the spend-guard's auditable estimate (labelled estimated:true);
    # sovereign tier -> $0.00 vendor charge with an explicit basis string.
    # Additive: only present once a route is served.
    cost: Optional[Dict[str, Any]] = None
    # Present only when the caller supplied a RequestBudget. Absent otherwise,
    # so unbudgeted provenance stays the previous shape.
    request_id: Optional[str] = None
    policy_revision: Optional[str] = None
    terminal_reason: Optional[str] = None
    attempt_ceiling: Optional[int] = None
    attempts_reserved: Optional[int] = None

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
        if self.cost is not None:
            d["cost"] = self.cost
        if self.request_id is not None:
            d["request_id"] = self.request_id
            d["policy_revision"] = self.policy_revision
            d["terminal_reason"] = self.terminal_reason
            d["attempt_ceiling"] = self.attempt_ceiling
            d["attempts_reserved"] = self.attempts_reserved
        return d


def _tier_of(p: Provider) -> str:
    if p.sovereign:
        return "sovereign"
    if p.name == "openai":
        # Owner-qualified 2026-11-16 ("use the API with money"): exact pricing
        # reviewed, strict pre-call USD reservation implemented. NOT sovereign.
        re
turn "qualified-paid"
    if p.name == "moonshot":
        return "paid-grid"
    # Provider identity (or a :free model suffix) cannot prove model-specific
    # pricing, quota, account eligibility, or terms.
    return "unqualified-grid"


class RouterError(RuntimeError):
    def __init__(self, message: str, attempts: List[Attempt],
                 terminal_reason: Optional[str] = None):
        super().__init__(message)
        self.attempts = attempts
        self.terminal_reason = terminal_reason


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

# Set only around a budgeted dispatch. While set, request_json must not send
# a second request on its own: that resend would be an uncounted attempt.
_REQUEST_BUDGET: "contextvars.ContextVar[Optional[RequestBudget]]" = contextvars.ContextVar(
    "szl_request_budget", default=None,
)


class _ConnectionPool:
    def __init__(self, max_per_host: int = _POOL_MAX_PER_HOST,
                 max_hosts: int = _POOL_MAX_HOSTS):
        self._max_per_host = max_per_host
        self._max_hosts = max_hosts
        self._idle: Dict[Tuple[str, str, int], List[http.client.HTTPConnection]
] = {}
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
                # http.client applies HTTPConnection.timeout when the socket is
                # created. A kept-alive socket keeps its previous timeout unless
                # it is updated here. Connect and later reads share this one
                # socket timeout; it is not a separate overall deadline.
                sock = getattr(conn, "sock", None)
                if sock is not None:
                    try:
                        sock.settimeout(timeout)
                    except OSError:
                        pass
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

    def request_json(self, url: str, data: bytes, headers: Dict
[str, str],
                     timeout: float) -> Dict[str, Any]:
        """POST `data` to `url` and return the parsed JSON body, reusing a pooled
        keep-alive connection when one is available.

        Raises urllib.error.HTTPError on a non-2xx status so the existing
        failover/honesty handling in chat()/embed() is unchanged.

        `timeout` is the socket timeout for both the connection attempt and
        later blocking reads. http.client does not take separate connect and
        read deadlines. It is not an overall request deadline. A timeout does
        not prove the peer cancelled work that may already have been sent.
        When a RequestBudget is active, a failed first send is not repeated
        here; the budget has to reserve any later dispatch."""
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
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
                if _REQUEST_BUDGET.get() is not None:
                    # The failed send may have left the machine. Do not hide a
                    # second dispatch inside the pool. The request budget records
                    # the outcome as unknown and may reserve another attempt.
                    raise
                # Unbudgeted callers keep the previous behavior: a stale pooled
                # connection can fail at send/recv and is retried once on a fresh
                # connection. That resend is not a separate attempt coun
ter.
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
# FULL jitter
 (sleep = random(0, base * 2**attempt), capped). Full jitter is the
# AWS-recommended shape: it spreads concurrent clients so they don't retry in
# lockstep and stampede a recovering upstream.
#
# That per-provider cap remains the default. A caller that passes a
# RequestBudget uses one shared attempt ceiling and one absolute monotonic
# deadline for the initial send, every retry, and every fallback. The budget
# does not reserve USD and does not qualify a paid or unqualified route.
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


def _latency_ms(now: float, started: float) -> int:
    try:
        return max(0, int((now - started) * 1000))
    except (TypeError, ValueError, OverflowError):
        return 
0


def _http_error_text(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8")[:200]
    except Exception:  # noqa: BLE001
        return str(exc)[:200]


def _transport_attempt(provider: Provider, payload: Dict[str, Any], *,
                       ok: bool, status: Optional[int], error: Optional[str],
                       latency_ms: int) -> Attempt:
    name = getattr(provider, "name", "") or ""
    model = ""
    if isinstance(payload, dict) and isinstance(payload.get("model"), str):
        model = payload["model"]
    return Attempt(name, model, ok, status=status, error=error, latency_ms=latency_ms)


def _post_with_retry(poster, provider: Provider, payload: Dict[str, Any],
                     timeout: float, budget: Optional[RequestBudget] = None,
                     on_attempt: Optional[Callable[[Attempt], None]] = None):
    """Call `poster(provider, payload, timeout)` with same-provider transient
    retry (exponential backoff + full jitter).

    Without `budget`, each call has its own cap of `_RETRY_MAX_ATTEMPTS`.
    The caller's existing per-route handling records one attempt row for the
    whole sequence, as before.

    With `budget`, every initiated dispatch reserves one attempt on that
    shared counter, including later providers and retries. `on_attempt` is
    called once per initiated dispatch. The pool's hidden stale-connection
    resend is disabled for the duration of the poster call.
    """
    if budget is None:
        if on_attempt is not None:
            raise ValueError("on_attempt requires a request budget")
        last_exc: Exception
        for attempt in range(_RETRY_MAX_ATTEMPTS):
            try:
                return poster(provider, payload, timeout)
            except urllib.error.HTTPError as e:
                last_exc = e
                if not _is_retryable_http(getattr(e, "code", None)):
                    raise  # permanent (e.g. 400/401/404) — fail through now
            excep
t (http.client.HTTPException, OSError) as e:
                last_exc = e  # connection-level blip — retryable
            if attempt + 1 >= _RETRY_MAX_ATTEMPTS:
                break
            time.sleep(_backoff_sleep_seconds(attempt))
        raise last_exc

    last_budget_exc: Optional[Exception] = None
    while True:
        try:
            attempt_no, send_timeout = budget.reserve(timeout)
        except BudgetStop:
            if last_budget_exc is not None:
                raise last_budget_exc
            raise
        started = budget.clock()
        token = _REQUEST_BUDGET.set(budget)
        try:
            try:
                result = poster(provider, payload, send_timeout)
            except urllib.error.HTTPError as e:
                last_budget_exc = e
                budget.note_outcome(attempt_no, "known")
                if on_attempt is not None:
                    on_attempt(_transport_attempt(
                        provider, payload, ok=False, status=getattr(e, "code", None),
                        error=_http_error_text(e),
                        latency_ms=_latency_ms(budget.clock(), started),
                    ))
                if not _is_retryable_http(getattr(e, "code", None)):
                    raise
            except (http.client.HTTPException, OSError) as e:
                last_budget_exc = e
                # The peer may have received the request. Do not call this known.
                budget.note_outcome(attempt_no, "unknown")
                if on_attempt is not None:
                    on_attempt(_transport_attempt(
                        provider, payload, ok=False, status=None,
                        error=f"{type(e).__name__}: {e}"[:200],
                        latency_ms=_latency_ms(budget.clock(), started),
                    ))
            except Exception as e:
                last_budget_exc = e
                budget.note_outcome(attempt_no, "unknown")
                if on_attempt is not None:
  
                  on_attempt(_transport_attempt(
                        provider, payload, ok=False, status=None,
                        error=f"{type(e).__name__}: {e}"[:200],
                        latency_ms=_latency_ms(budget.clock(), started),
                    ))
                raise
            else:
                budget.note_outcome(attempt_no, "known")
                if on_attempt is not None:
                    on_attempt(_transport_attempt(
                        provider, payload, ok=True, status=200, error=None,
                        latency_ms=_latency_ms(budget.clock(), started),
                    ))
                return result
        finally:
            _REQUEST_BUDGET.reset(token)
        delay = _backoff_sleep_seconds(attempt_no - 1)
        if not budget.allow_backoff(delay):
            if last_budget_exc is None:
                raise BudgetStop(budget.terminal or "attempt_exhausted", budget.in_flight)
            raise last_budget_exc
        budget.sleep(delay)


# ---------------------------------------------------------------------------
# Per-upstream failure cooldown (LiteLLM-inspired, made honesty-first).
#
# After a provider fails a route (retry budget spent or permanent error) we put
# it on a SHORT cooldown so the next requests don't pay the same failure
# latency again. Honesty rules:
#   * a cooled provider is only SKIPPED while a WARM candidate (available and
#     not cooling) remains later in the chain — if it is the last real option
#     we still try it, because trying loudly beats refusing silently;
#   * every skip is recorded in the attempt trail ("cooldown-skip ...") so the
#     signed receipt shows exactly why a provider was not consulted;
#   * a successful call clears that provider's cooldown immediately.
# SZL_COOLDOWN_SECONDS=0 disables the mechanism (default 30s).
# ---------------------------------------------------------------------------
_COOLDOWN_LOCK = threading.Lock()
_COOLDOWN_UNTIL: Dict[str
, float] = {}


def _cooldown_seconds() -> float:
    try:
        v = os.environ.get("SZL_COOLDOWN_SECONDS", "").strip()
        return float(v) if v else 30.0
    except Exception:
        return 30.0


def _cooldown_remaining(provider_name: str) -> float:
    with _COOLDOWN_LOCK:
        until = _COOLDOWN_UNTIL.get(provider_name, 0.0)
    return max(0.0, until - time.time())


def _set_cooldown(provider_name: str) -> None:
    secs = _cooldown_seconds()
    if secs <= 0:
        return
    with _COOLDOWN_LOCK:
        _COOLDOWN_UNTIL[provider_name] = time.time() + secs


def _clear_cooldown(provider_name: str) -> None:
    with _COOLDOWN_LOCK:
        _COOLDOWN_UNTIL.pop(provider_name, None)


def _warm_candidate_later(routes: List[Route],

... [Content truncated]