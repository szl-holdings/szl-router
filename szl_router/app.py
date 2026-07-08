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

Auth: if SZL_ROUTER_TOKEN is set, callers must send Authorization: Bearer <it>.
Upstream keys are read from the environment by core.py and never logged.
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import core, receipts, spend_guard


@asynccontextmanager
async def _lifespan(_app: "FastAPI"):
    # Resolve (or generate ephemeral) the receipt signing key once, logging the
    # honest posture + public key so a caller can verify this session's receipts.
    receipts.init_signing()
    yield


app = FastAPI(title="SZL Router", version="1.0", lifespan=_lifespan)


def _check_auth(request: Request) -> None:
    want = os.environ.get("SZL_ROUTER_TOKEN", "").strip()
    if not want:
        return
    got = request.headers.get("authorization", "")
    if got != f"Bearer {want}":
        raise HTTPException(status_code=401, detail="invalid SZL_ROUTER_TOKEN")


@app.get("/healthz")
def healthz() -> Dict[str, Any]:
    return {"ok": True, "service": "szl-router", "ts": int(time.time())}


@app.get("/status")
def status() -> Dict[str, Any]:
    return core.status()


@app.get("/v1/spend")
def spend_state() -> Dict[str, Any]:
    """Honest read-only view of the local spend cap + kill-switch + append-only
    ledger this router enforces at the paid-tier chokepoint. Loopback-only."""
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
    body = await request.json()
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
    envelope = receipts.build_envelope(
        provenance=prov,
        model=model,
        usage=result.get("usage"),
        req_digest=receipts.request_digest(model, messages),
        # For "szl-auto" this signs the routing decision INTO the receipt; None
        # (hence omitted) for every other model, keeping their receipts unchanged.
        routing=prov.get("routing"),
    )
    if envelope is not None:
        headers["x-szl-receipt"] = receipts.encode_header(envelope)
    return JSONResponse(content=result, headers=headers)


@app.post("/v1/receipt/verify")
async def receipt_verify(request: Request) -> Dict[str, Any]:
    """Independently verify a receipt a buyer received. Body:
    {"envelope": <x-szl-receipt envelope dict OR base64 header string>,
     "public_key_pem": "<PEM>"}. public_key_pem is optional — omit it to confirm
    the UNSIGNED-honest verdict. Returns {valid, detail, signed}. Honest by
    construction: a keyless envelope always returns valid=false/'unsigned-honest',
    and tampering with the body fails verification."""
    body = await request.json()
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
    body = await request.json()
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
    except core.RouterError as e:
        return JSONResponse(
            status_code=502,
            content={
                "error": {"message": str(e), "type": "router_all_routes_failed"},
                "x_szl_provenance": {"attempts": [a.__dict__ for a in e.attempts]},
            },
        )
    return JSONResponse(content=result)


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
