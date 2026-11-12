"""SZL Router — OpenAI-compatible HTTP surface over core.py.

Run:
    pip install -r requirements.txt
    uvicorn szl_router.app:app --host 0.0.0.0 --port 8099

Endpoints:
    POST /v1/chat/completions   OpenAI-compatible; body.model = szl-large|szl-fast|szl-coder
                                or provider:upstream_model. Response carries an
                                added x_szl_provenance block.
    GET  /v1/models             lists our logical models (OpenAI shape).
    GET  /status                honest provider/route snapshot.
    GET  /healthz               liveness.

Auth: callers must send Authorization: Bearer <SZL_ROUTER_TOKEN>. With no token
configured the chat/embeddings/spend endpoints fail closed (503) unless the
operator explicitly opts in to open mode with SZL_ROUTER_ALLOW_NO_AUTH=1.
Responses report a provider's base_url only for well-known public provider
hosts; operator-configured endpoints read "redacted" (set
SZL_ROUTER_EXPOSE_BASE_URL=1 to restore the previous behaviour).
Upstream keys are read from the environment by core.py and never logged.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import math
import os
import sys
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import core, grid, receipts, spend_guard


@asynccontextmanager
async def _lifespan(_app: "FastAPI"):
    # Resolve (or generate ephemeral) the receipt signing key once, logging the
    # honest posture + public key so a caller can verify this session's receipts.
    receipts.init_signing()
    if not os.environ.get("SZL_ROUTER_TOKEN", "").strip():
        if _env_flag(ALLOW_NO_AUTH_ENV):
            print("szl-router: WARNING running OPEN (no SZL_ROUTER_TOKEN, "
                  f"{ALLOW_NO_AUTH_ENV}=1)", file=sys.stderr, flush=True)
        else:
            print("szl-router: no SZL_ROUTER_TOKEN set; chat/embeddings/spend "
                  f"will answer 503 until it is (or {ALLOW_NO_AUTH_ENV}=1)", file=sys.stderr, flush=True)
    yield


app = FastAPI(title="SZL Router", version="1.0", lifespan=_lifespan)


ALLOW_NO_AUTH_ENV = "SZL_ROUTER_ALLOW_NO_AUTH"
EXPOSE_BASE_URL_ENV = "SZL_ROUTER_EXPOSE_BASE_URL"
REDACTED = "redacted"


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip() == "1"


def _check_auth(request: Request) -> None:
    want = os.environ.get("SZL_ROUTER_TOKEN", "").strip()
    if not want:
        if _env_flag(ALLOW_NO_AUTH_ENV):
            return
        # Fail closed: an unset token is a deployment mistake, not an open door.
        raise HTTPException(
            status_code=503,
            detail="szl-router auth is not configured: set SZL_ROUTER_TOKEN "
                   f"(or {ALLOW_NO_AUTH_ENV}=1 to run open deliberately)",
        )
    got = request.headers.get("authorization", "")
    if not hmac.compare_digest(got.encode("utf-8"), f"Bearer {want}".encode("utf-8")):
        raise HTTPException(status_code=401, detail="invalid SZL_ROUTER_TOKEN")


def _public_provider_hosts() -> frozenset:
    hosts = set()
    for p in core.PROVIDERS.values():
        default = getattr(p, "base_url_default", "") or ""
        parts = urlsplit(default)
        if parts.scheme == "https" and parts.hostname:
            hosts.add(parts.hostname.lower())
    return frozenset(hosts)


def _redact_base_url(url: Any) -> Any:
    """Keep base_url only for well-known public provider hosts (https, built-in
    default host). Operator-configured endpoints (own nodes, private or tailnet
    addresses) are reported as "redacted" so a response never maps the fabric."""
    if url is None or _env_flag(EXPOSE_BASE_URL_ENV):
        return url
    if not isinstance(url, str):
        return REDACTED
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme == "https" and host and host in _public_provider_hosts() \
            and parts.username is None and parts.password is None:
        return url
    return REDACTED


def _redact_provenance(result: Any) -> Any:
    if isinstance(result, dict):
        prov = result.get("x_szl_provenance")
        if isinstance(prov, dict) and "base_url" in prov:
            prov["base_url"] = _redact_base_url(prov["base_url"])
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


def _finite_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    return number


async def _json_object(request: Request) -> Dict[str, Any]:
    """Reject malformed or non-object JSON before endpoint field access."""
    try:
        body = json.loads(await request.body(), parse_constant=_reject_json_constant,
                          parse_float=_finite_json_float)
    except (ValueError, RecursionError) as exc:
        raise HTTPException(
            status_code=400,
            detail="request body must be finite JSON within parser limits",
        ) from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    return body


@app.get("/healthz")
def healthz() -> Dict[str, Any]:
    return {"ok": True, "service": "szl-router", "ts": int(time.time())}


@app.get("/status")
def status() -> Dict[str, Any]:
    snap = core.status()
    for row in snap.get("providers", []):
        if isinstance(row, dict) and "base_url" in row:
            row["base_url"] = _redact_base_url(row["base_url"])
    return snap


@app.get("/v1/spend")
def spend_state(request: Request) -> Dict[str, Any]:
    """Read-only legacy advisory ledger and kill-file state. Paid cloud routing
    is blocked before transport; this is not an active spend cap. Loopback-only."""
    _check_auth(request)
    try:
        peer = ipaddress.ip_address(request.client.host) if request.client else None
    except ValueError:
        peer = None
    if isinstance(peer, ipaddress.IPv6Address) and peer.ipv4_mapped is not None:
        peer = peer.ipv4_mapped
    if peer is None or not peer.is_loopback:
        raise HTTPException(status_code=403, detail="spend state is available to loopback peers only")
    return spend_guard.state()


@app.get("/fabric")
@app.get("/energy")
def fabric() -> Dict[str, Any]:
    """Energy/sovereignty posture of the whole fabric (Sovereign-Resilience ladder)
    with the live wasted-energy harvest overlay (R-HARVEST-FABRIC)."""
    return core.fabric_status(include_harvest=True, allow_network=True)


@app.get("/harvest")
def harvest() -> Dict[str, Any]:
    """Live wasted-energy harvest posture from real public grid feeds (no key):
    negative wholesale price (aWATTar DE), renewable-share curtailment
    (Energy-Charts), carbon intensity (UK). Honest: a down feed reads
    'unreachable', joules stay SAMPLE, grid data never sets sovereign:true."""
    return core.harvest_status(allow_network=True)


@app.get("/v1/models")
def models() -> Dict[str, Any]:
    now = int(time.time())
    data = [{"id": name, "object": "model", "created": now, "owned_by": "szl-router"}
            for name in core.MODEL_ROUTES]
    # Opt-in smart-routing pseudo-model: scores prompt complexity and dispatches
    # sovereign-first to a real logical model, recording the decision in the receipt.
    data.append({"id": core.AUTO_MODEL, "object": "model", "created": now,
                 "owned_by": "szl-router"})
    # Embeddings models (HOME-node-first) are listed too so callers can discover them.
    data += [{"id": name, "object": "model", "created": now, "owned_by": "szl-router"}
             for name in core.EMBED_ROUTES]
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> JSONResponse:
    _check_auth(request)
    body = await _json_object(request)
    messages: Optional[List[Dict[str, Any]]] = body.get("messages")
    if not messages:
        raise HTTPException(status_code=400, detail="messages is required")
    model = body.get("model") or core.DEFAULT_MODEL
    try:
        result = core.chat(
            model,
            messages,
            temperature=body.get("temperature"),
            max_tokens=body.get("max_tokens"),
            extra={k: v for k, v in body.items()
                   if k not in {"model", "messages", "temperature", "max_tokens", "stream"}},
        )
    except core.RouterError as e:
        return JSONResponse(
            status_code=502,
            content={
                "error": {"message": str(e), "type": "router_all_routes_failed"},
                "x_szl_provenance": {"attempts": [a.__dict__ for a in e.attempts]},
            },
        )
    headers: Dict[str, str] = {}
    prov = result.get("x_szl_provenance", {})
    # Observer frame (R3 doctrine: a receipt is honest RELATIVE to who asked and
    # how they were authenticated). No PII, no key material — just the frame.
    observer = {
        "endpoint": "/v1/chat/completions",
        "auth_mode": "bearer" if os.environ.get("SZL_ROUTER_TOKEN", "").strip() else "open",
        "requested_model": model,
        "note": ("observer frame: what this caller asked for and how they were "
                 "authenticated — the receipt verdict is relative to this frame"),
    }
    # OPTIONAL, OPT-IN, NON-BLOCKING honest grid_context (SZL_RECEIPT_GRID_CONTEXT).
    # Reads a short-TTL cache warmed by a background daemon — the keyless UK Carbon
    # Intensity fetch NEVER runs on this request path, so routing latency is
    # untouched. Returns None (block omitted -> receipt byte-identical) until the
    # cache is warm; a REPORTED pass-through once warm; an honest UNAVAILABLE block
    # if the feed is unreachable. It DOCUMENTS the grid window and never measures a
    # joule (energy stays "UNAVAILABLE" in the receipt).
    grid_context = grid.current_grid_context() if grid.enabled() else None
    envelope = receipts.build_envelope(
        provenance=prov,
        model=model,
        usage=result.get("usage"),
        req_digest=receipts.request_digest(model, messages),
        # For "szl-auto" this signs the routing decision INTO the receipt; None
        # (hence omitted) for every other model, keeping their receipts unchanged.
        routing=prov.get("routing"),
        # Honest cost + observer frame, signed when a key is armed (additive).
        cost=prov.get("cost"),
        observer=observer,
        grid_context=grid_context,
    )
    if envelope is not None:
        headers["x-szl-receipt"] = receipts.encode_header(envelope)
    # Redact after the receipt is built (base_url is not a receipt field, so
    # signed receipts are byte-identical either way).
    return JSONResponse(content=_redact_provenance(result), headers=headers)


@app.post("/v1/receipt/verify")
async def receipt_verify(request: Request) -> Dict[str, Any]:
    """Independently verify a receipt a buyer received. Body:
    {"envelope": <x-szl-receipt envelope dict OR base64 header string>,
     "public_key_pem": "<PEM>"}. public_key_pem is optional — omit it to confirm
    the UNSIGNED-honest verdict. Returns {valid, detail, signed}. Honest by
    construction: a keyless envelope always returns valid=false/'unsigned-honest',
    and tampering with the body fails verification."""
    body = await _json_object(request)
    env = body.get("envelope")
    if env is None:
        raise HTTPException(status_code=400, detail="envelope is required")
    if isinstance(env, str):
        try:
            env = receipts.decode_header(env)
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="envelope is not valid base64-JSON")
    if not isinstance(env, dict):
        raise HTTPException(status_code=400, detail="envelope must be an object or base64 string")
    valid, detail = receipts.verify_envelope(env, body.get("public_key_pem"))
    return {"valid": valid, "detail": detail, "signed": bool(env.get("signed", False))}


@app.get("/v1/receipt/pubkey")
def receipt_pubkey() -> Dict[str, Any]:
    """The PUBLIC key this session signs receipts with (PEM), so a buyer can
    verify x-szl-receipt headers independently. Honest about provenance: reports
    whether the key is an ephemeral session key or a configured one, and null if
    receipts are unsigned (keyless / szl-receipt absent)."""
    receipts.init_signing()
    st = receipts.signing_state()
    return {
        "public_key_pem": receipts.public_key_pem(),
        "source": st.source,
        "ephemeral": st.ephemeral,
        "library_available": st.library_available,
        "note": ("ephemeral session key — verifies THIS session's receipts only; "
                 "not a persistent identity" if st.ephemeral else
                 "configured signing key" if st.private_pem else
                 "receipts are UNSIGNED-honest (no key armed)"),
    }


@app.post("/v1/embeddings")
async def embeddings(request: Request) -> JSONResponse:
    """OpenAI-compatible embeddings. body.model = bge-large (HOME-node-first) or
    provider:upstream_model. Same honest provenance contract as chat: the answer
    carries x_szl_provenance (served_by, sovereign true ONLY for owned metal, tier,
    full attempts trail). Sequential failover across separate sovereign workers —
    never fused VRAM. Fails loud (502) with the attempt trail if no route answers."""
    _check_auth(request)
    body = await _json_object(request)
    input_ = body.get("input")
    if input_ is None:
        raise HTTPException(status_code=400, detail="input is required")
    model = body.get("model") or "bge-large"
    try:
        result = core.embed(
            model,
            input_,
            extra={k: v for k, v in body.items()
                   if k not in {"model", "input"}},
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except core.RouterError as e:
        return JSONResponse(
            status_code=502,
            content={
                "error": {"message": str(e), "type": "router_all_routes_failed"},
                "x_szl_provenance": {"attempts": [a.__dict__ for a in e.attempts]},
            },
        )
    return JSONResponse(content=_redact_provenance(result))


def main() -> None:
    """Module entrypoint so the service is runnable as `python -m szl_router.app`
    (this is what the Docker image runs). Host/port are env-overridable; the
    Docker default is 0.0.0.0:8000."""
    import uvicorn
    host = os.environ.get("SZL_ROUTER_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", os.environ.get("SZL_ROUTER_PORT", "8000")))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
