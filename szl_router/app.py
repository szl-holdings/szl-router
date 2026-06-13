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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import core

app = FastAPI(title="SZL Router", version="1.0")


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


@app.get("/v1/models")
def models() -> Dict[str, Any]:
    now = int(time.time())
    data = [{"id": name, "object": "model", "created": now, "owned_by": "szl-router"}
            for name in core.MODEL_ROUTES]
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
    return JSONResponse(content=result)
