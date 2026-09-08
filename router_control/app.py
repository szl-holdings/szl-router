#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Sovereign-first, OpenAI-compatible routing gateway with exact receipts.

Egress is disabled by default. An operator must supply a validated provider
registry, an exact hostname allowlist, and SZL_ROUTER_ENABLE_EGRESS=1. Secrets
are read only from named environment variables and never returned or logged.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

APP_VERSION = "1.0.0"
SOURCE_SCHEMA = "szl.router-source/v1"
PLAN_SCHEMA = "szl.router-plan/v1"
RECEIPT_SCHEMA = "szl.router-receipt/v1"
MAX_PROVIDERS = 16
MAX_MODELS_PER_PROVIDER = 128
MAX_MESSAGES = 64
MAX_CONTENT_CHARS = 32_000
MAX_TOTAL_CONTENT_CHARS = 96_000
MAX_RESPONSE_BYTES = 2_000_000
MAX_TIMEOUT_SECONDS = 45.0
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")
TOKEN_ENV = re.compile(r"^[A-Z][A-Z0-9_]{2,95}$")
CLASSIFICATIONS = {"public", "internal", "confidential", "restricted"}
ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().parent / "static"


def canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")


def sha256(value: Any) -> str:
    data = value if isinstance(value, bytes) else canonical(value)
    return hashlib.sha256(data).hexdigest()


def source_revision() -> str:
    value = (
        os.getenv("SOURCE_REVISION")
        or os.getenv("GIT_COMMIT")
        or ""
    ).strip().lower()
    return value if re.fullmatch(r"[0-9a-f]{40,64}", value) else "UNAVAILABLE"


def enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def normalize_host(value: str) -> str:
    return value.strip().lower().rstrip(".")


def parse_allowed_hosts() -> frozenset[str]:
    return frozenset(
        normalize_host(item)
        for item in os.getenv("SZL_ROUTER_ALLOWED_HOSTS", "").split(",")
        if normalize_host(item)
    )


def validate_base_url(value: str, allowed_hosts: frozenset[str]) -> str:
    parsed = urllib.parse.urlsplit(value.strip())
    host = normalize_host(parsed.hostname or "")
    if parsed.scheme != "https":
        raise ValueError("provider base_url must use https")
    if not host or host not in allowed_hosts:
        raise ValueError("provider hostname is not in SZL_ROUTER_ALLOWED_HOSTS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("provider base_url cannot contain credentials, query, or fragment")
    if parsed.port not in {None, 443}:
        raise ValueError("provider base_url may use only the default HTTPS port")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        raise ValueError("literal IP provider hosts are forbidden")
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("localhost provider hosts are forbidden")
    path = parsed.path.rstrip("/")
    if path and not path.startswith("/"):
        raise ValueError("invalid provider base path")
    return urllib.parse.urlunsplit(("https", host, path, "", ""))


class ProviderRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=96, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")
    base_url: str = Field(min_length=9, max_length=512)
    models: dict[str, str] = Field(min_length=1, max_length=MAX_MODELS_PER_PROVIDER)
    token_env: str = Field(min_length=3, max_length=96, pattern=r"^[A-Z][A-Z0-9_]{2,95}$")
    priority: int = Field(default=100, ge=0, le=10_000)
    sovereignty: int = Field(default=0, ge=0, le=100)
    cost_tier: int = Field(default=1, ge=0, le=10)
    classifications: list[str] = Field(default_factory=lambda: ["public"], min_length=1, max_length=4)
    enabled: bool = True

    @field_validator("models")
    @classmethod
    def valid_models(cls, models: dict[str, str]) -> dict[str, str]:
        for public, upstream in models.items():
            if not IDENTIFIER.fullmatch(public) or not IDENTIFIER.fullmatch(upstream):
                raise ValueError("model aliases must use bounded identifiers")
        return dict(sorted(models.items()))

    @field_validator("classifications")
    @classmethod
    def valid_classifications(cls, values: list[str]) -> list[str]:
        normalized = [value.lower() for value in values]
        invalid = sorted(set(normalized) - CLASSIFICATIONS)
        if invalid:
            raise ValueError(f"unsupported classifications: {invalid}")
        if len(normalized) != len(set(normalized)):
            raise ValueError("classification values must be unique")
        return sorted(normalized)


class Registry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    providers: list[ProviderRecord] = Field(default_factory=list, max_length=MAX_PROVIDERS)

    @model_validator(mode="after")
    def unique_ids(self) -> "Registry":
        ids = [provider.id for provider in self.providers]
        if len(ids) != len(set(ids)):
            raise ValueError("provider IDs must be unique")
        return self


@dataclass(frozen=True)
class Settings:
    registry: Registry
    allowed_hosts: frozenset[str]
    egress_enabled: bool
    config_state: str
    config_error: str | None = None


def load_settings() -> Settings:
    allowed_hosts = parse_allowed_hosts()
    egress = enabled("SZL_ROUTER_ENABLE_EGRESS")
    raw = os.getenv("SZL_ROUTER_PROVIDERS_JSON", "").strip()
    if not raw:
        return Settings(Registry(), allowed_hosts, egress, "UNAVAILABLE_NOT_CONFIGURED")
    try:
        parsed = json.loads(raw)
        registry = Registry.model_validate(parsed)
        normalized: list[ProviderRecord] = []
        for provider in registry.providers:
            normalized.append(
                provider.model_copy(
                    update={"base_url": validate_base_url(provider.base_url, allowed_hosts)}
                )
            )
        return Settings(
            Registry(providers=normalized),
            allowed_hosts,
            egress,
            "VALIDATED",
        )
    except (json.JSONDecodeError, ValueError):
        return Settings(
            Registry(),
            allowed_hosts,
            False,
            "INVALID_FAIL_CLOSED",
            "Provider configuration is invalid; check the registry schema and hostname allowlist.",
        )


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=96, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")
    data_classification: str = Field(default="public")
    max_cost_tier: int = Field(default=10, ge=0, le=10)

    @field_validator("data_classification")
    @classmethod
    def classification(cls, value: str) -> str:
        normalized = value.lower()
        if normalized not in CLASSIFICATIONS:
            raise ValueError("unsupported data classification")
        return normalized


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["system", "user", "assistant", "tool"]
    content: str = Field(min_length=1, max_length=MAX_CONTENT_CHARS)
    name: str | None = Field(default=None, max_length=96, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=96, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")
    messages: list[Message] = Field(min_length=1, max_length=MAX_MESSAGES)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, gt=0.0, le=1.0)
    max_tokens: int | None = Field(default=None, ge=1, le=131_072)
    stream: bool = False
    stop: str | list[str] | None = None
    user: str | None = Field(default=None, max_length=128)
    data_classification: str = Field(default="public")
    max_cost_tier: int = Field(default=10, ge=0, le=10)

    @field_validator("data_classification")
    @classmethod
    def classification(cls, value: str) -> str:
        normalized = value.lower()
        if normalized not in CLASSIFICATIONS:
            raise ValueError("unsupported data classification")
        return normalized

    @field_validator("stop")
    @classmethod
    def bounded_stop(cls, value: str | list[str] | None) -> str | list[str] | None:
        values = [value] if isinstance(value, str) else value or []
        if len(values) > 8 or any(len(item) > 256 for item in values):
            raise ValueError("stop sequence bounds exceeded")
        return value

    @model_validator(mode="after")
    def total_content(self) -> "ChatRequest":
        if sum(len(message.content) for message in self.messages) > MAX_TOTAL_CONTENT_CHARS:
            raise ValueError(f"total message content exceeds {MAX_TOTAL_CONTENT_CHARS} characters")
        if self.stream:
            raise ValueError("streaming is unavailable in receipt-verified v1")
        return self


def route_candidates(settings: Settings, request: PlanRequest) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for provider in settings.registry.providers:
        if not provider.enabled:
            continue
        upstream_model = provider.models.get(request.model)
        if not upstream_model:
            continue
        if request.data_classification not in provider.classifications:
            continue
        if provider.cost_tier > request.max_cost_tier:
            continue
        candidates.append(
            {
                "provider_id": provider.id,
                "public_model": request.model,
                "upstream_model": upstream_model,
                "priority": provider.priority,
                "sovereignty": provider.sovereignty,
                "cost_tier": provider.cost_tier,
                "classification": request.data_classification,
                "credential_state": "AVAILABLE" if os.getenv(provider.token_env) else "UNAVAILABLE",
            }
        )
    candidates.sort(
        key=lambda item: (
            -item["sovereignty"],
            item["priority"],
            item["cost_tier"],
            item["provider_id"],
        )
    )
    return candidates


def plan(settings: Settings, request: PlanRequest) -> dict[str, Any]:
    candidates = route_candidates(settings, request)
    body = {
        "schema": PLAN_SCHEMA,
        "model": request.model,
        "classification": request.data_classification,
        "max_cost_tier": request.max_cost_tier,
        "registry_state": settings.config_state,
        "egress_enabled": settings.egress_enabled,
        "candidates": candidates,
        "selected": candidates[0]["provider_id"] if candidates else None,
        "selection_algorithm": "sovereignty-desc_priority-asc_cost-asc_id-asc/v1",
    }
    return {
        **body,
        "receipt": {
            "algorithm": "sha256",
            "digest": sha256(body),
            "canonical_bytes": len(canonical(body)),
        },
    }


def provider_by_id(settings: Settings, provider_id: str) -> ProviderRecord:
    for provider in settings.registry.providers:
        if provider.id == provider_id:
            return provider
    raise LookupError(provider_id)


def upstream_payload(request: ChatRequest, upstream_model: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": upstream_model,
        "messages": [message.model_dump(mode="json", exclude_none=True) for message in request.messages],
        "stream": False,
    }
    for field in ("temperature", "top_p", "max_tokens", "stop", "user"):
        value = getattr(request, field)
        if value is not None:
            payload[field] = value
    return payload


async def call_provider(provider: ProviderRecord, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    token = os.getenv(provider.token_env, "")
    if not token:
        raise RuntimeError("credential unavailable")
    url = provider.base_url.rstrip("/") + "/chat/completions"
    timeout = httpx.Timeout(MAX_TIMEOUT_SECONDS, connect=10.0)
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
    ) as client:
        async with client.stream(
            "POST",
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "szl-router/1.0",
            },
            json=payload,
        ) as response:
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise RuntimeError("upstream response exceeded byte limit")
                chunks.append(chunk)
            raw = b"".join(chunks)
            if response.status_code >= 400:
                raise httpx.HTTPStatusError(
                    f"upstream status {response.status_code}",
                    request=response.request,
                    response=response,
                )
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("upstream response was not strict UTF-8 JSON") from exc
            if not isinstance(value, dict):
                raise RuntimeError("upstream response must be a JSON object")
            return value, response.status_code


def public_registry(settings: Settings) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for provider in settings.registry.providers:
        rows.append(
            {
                "id": provider.id,
                "models": sorted(provider.models),
                "priority": provider.priority,
                "sovereignty": provider.sovereignty,
                "cost_tier": provider.cost_tier,
                "classifications": provider.classifications,
                "enabled": provider.enabled,
                "credential_state": "AVAILABLE" if os.getenv(provider.token_env) else "UNAVAILABLE",
                "endpoint_state": "VALIDATED_ALLOWLISTED",
            }
        )
    return rows


app = FastAPI(
    title="SZL Sovereign Router",
    version=APP_VERSION,
    docs_url="/api/docs",
    redoc_url=None,
)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def no_store_evidence(request: Request, call_next):
    response = await call_next(request)
    if not request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def inference_readiness(settings: Settings) -> dict[str, Any]:
    """Configuration admission only: no network call or inference proof."""
    checks = {
        "registry_valid": settings.config_state == "VALIDATED",
        "egress_enabled": settings.egress_enabled,
        "caller_auth_configured": bool(os.getenv("SZL_ROUTER_TOKEN", "").strip()),
        "credentialed_provider": any(
            provider.enabled and bool(os.getenv(provider.token_env, "").strip())
            for provider in settings.registry.providers
        ),
    }
    admitted = all(checks.values())
    return {
        "status": "configured" if admitted else "unavailable",
        "checks": checks,
        "ready_for_requests": admitted,
        "basis": "LOCAL_CONFIGURATION_ONLY",
        "provider_reachability": "UNVERIFIED",
        "inference_witness": "UNAVAILABLE",
    }


def check_caller_auth(request: Request) -> None:
    expected = os.getenv("SZL_ROUTER_TOKEN", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail={"code": "CALLER_AUTH_NOT_CONFIGURED"})
    received = request.headers.get("Authorization", "")
    if not secrets.compare_digest(received.encode("utf-8"), f"Bearer {expected}".encode("utf-8")):
        raise HTTPException(
            status_code=401,
            detail={"code": "INVALID_ROUTER_CREDENTIAL"},
            headers={"WWW-Authenticate": "Bearer"},
        )


def validate_completion(value: Any) -> None:
    """An HTTP success alone cannot establish a usable completion."""
    if not isinstance(value, dict) or value.get("error") is not None:
        raise RuntimeError("upstream completion must be an answer object")
    choices = value.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("upstream completion requires nonempty choices")
    for choice in choices:
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise RuntimeError("upstream completion requires assistant messages")
        if not any(key in message for key in ("content", "refusal", "tool_calls", "function_call", "audio")):
            raise RuntimeError("upstream assistant message has no output")
        for key in ("content", "refusal"):
            if message.get(key) is not None and not isinstance(message[key], str):
                raise RuntimeError("upstream output has invalid text fields")
        calls = message.get("tool_calls")
        if calls is not None and (
            not isinstance(calls, list)
            or any(not isinstance(call, dict) or not call for call in calls)
        ):
            raise RuntimeError("upstream output has invalid tool calls")


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"status": "ok", "service": "szl-router", "version": APP_VERSION}


@app.get("/readyz")
def readyz() -> JSONResponse:
    settings = load_settings()
    checks = {
        "static_index": (STATIC / "index.html").is_file(),
        "registry": settings.config_state in {"VALIDATED", "UNAVAILABLE_NOT_CONFIGURED"},
        "egress": (
            not settings.egress_enabled
            or (
                settings.config_state == "VALIDATED"
                and bool(settings.registry.providers)
                and bool(settings.allowed_hosts)
            )
        ),
    }
    status = "ready" if all(checks.values()) else "not-ready"
    code = 200 if status == "ready" else 503
    return JSONResponse(
        status_code=code,
        content={
            "status": status,
            "checks": checks,
            "registry_state": settings.config_state,
            "egress_enabled": settings.egress_enabled,
            "scope": "CONTROL_PLANE",
            "inference": inference_readiness(settings),
        },
    )


@app.get("/readyz/inference")
def readyz_inference() -> JSONResponse:
    admission = inference_readiness(load_settings())
    return JSONResponse(status_code=200 if admission["ready_for_requests"] else 503, content=admission)


@app.get("/.well-known/szl-source.json")
@app.get("/api/source")
def source() -> dict[str, Any]:
    controlled = [
        Path(__file__),
        STATIC / "index.html",
        STATIC / "app.js",
        STATIC / "styles.css",
    ]
    body = {
        "schema": SOURCE_SCHEMA,
        "repository": "szl-holdings/szl-router",
        "revision": source_revision(),
        "controlled_files": {
            path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in controlled
            if path.is_file()
        },
        "default_egress": False,
        "secret_output": False,
        "arbitrary_url_routing": False,
    }
    return {**body, "receipt": {"algorithm": "sha256", "digest": sha256(body)}}


@app.get("/api/routes")
def routes() -> dict[str, Any]:
    settings = load_settings()
    body = {
        "schema": "szl.router-registry/v1",
        "state": settings.config_state,
        "egress_enabled": settings.egress_enabled,
        "allowed_host_count": len(settings.allowed_hosts),
        "providers": public_registry(settings),
        "config_error": settings.config_error,
    }
    return {**body, "receipt": {"algorithm": "sha256", "digest": sha256(body)}}


@app.post("/api/plan")
def route_plan(request: PlanRequest) -> dict[str, Any]:
    return plan(load_settings(), request)


@app.get("/v1/models")
def models() -> dict[str, Any]:
    settings = load_settings()
    ids = sorted(
        {
            model
            for provider in settings.registry.providers
            if provider.enabled
            for model in provider.models
        }
    )
    return {
        "object": "list",
        "data": [
            {
                "id": model,
                "object": "model",
                "created": 0,
                "owned_by": "szl-router-registry",
            }
            for model in ids
        ],
        "szl_registry_state": settings.config_state,
    }


@app.post("/v1/chat/completions")
async def chat(request: ChatRequest, http_request: Request) -> JSONResponse:
    settings = load_settings()
    if settings.config_state == "INVALID_FAIL_CLOSED":
        raise HTTPException(status_code=503, detail={"code": "INVALID_ROUTER_CONFIGURATION", "message": settings.config_error})
    if not settings.egress_enabled:
        raise HTTPException(status_code=503, detail={"code": "EGRESS_DISABLED", "message": "Set an allowlisted registry and SZL_ROUTER_ENABLE_EGRESS=1."})

    check_caller_auth(http_request)

    request_plan = plan(
        settings,
        PlanRequest(
            model=request.model,
            data_classification=request.data_classification,
            max_cost_tier=request.max_cost_tier,
        ),
    )
    candidates = request_plan["candidates"]
    if not candidates:
        raise HTTPException(status_code=503, detail={"code": "NO_ELIGIBLE_PROVIDER", "message": "No configured provider satisfies model, classification, and cost policy."})

    request_digest = sha256(request.model_dump(mode="json"))
    attempts: list[dict[str, Any]] = []
    started = time.monotonic()
    for candidate in candidates:
        provider = provider_by_id(settings, candidate["provider_id"])
        if not os.getenv(provider.token_env):
            attempts.append({"provider_id": provider.id, "state": "SKIPPED_CREDENTIAL_UNAVAILABLE"})
            continue
        try:
            upstream, status_code = await call_provider(
                provider,
                upstream_payload(request, candidate["upstream_model"]),
            )
            validate_completion(upstream)
            elapsed_ms = round((time.monotonic() - started) * 1000, 3)
            attempts.append({"provider_id": provider.id, "state": "SUCCESS", "status_code": status_code})
            receipt_body = {
                "schema": RECEIPT_SCHEMA,
                "request_digest": request_digest,
                "plan_digest": request_plan["receipt"]["digest"],
                "provider_id": provider.id,
                "public_model": request.model,
                "upstream_model": candidate["upstream_model"],
                "classification": request.data_classification,
                "attempts": attempts,
                "elapsed_ms": elapsed_ms,
                "response_digest": sha256(upstream),
                "secret_material_recorded": False,
            }
            receipt = {**receipt_body, "digest": sha256(receipt_body), "algorithm": "sha256"}
            result = {**upstream, "szl_receipt": receipt}
            return JSONResponse(
                status_code=200,
                content=result,
                headers={
                    "X-SZL-Receipt": receipt["digest"],
                    "Cache-Control": "no-store",
                },
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            attempts.append({"provider_id": provider.id, "state": "UPSTREAM_HTTP_ERROR", "status_code": status})
            if 400 <= status < 500 and status != 429:
                break
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            attempts.append({"provider_id": provider.id, "state": "TRANSPORT_OR_CONTRACT_ERROR", "error_type": type(exc).__name__})

    failure = {
        "schema": RECEIPT_SCHEMA,
        "request_digest": request_digest,
        "plan_digest": request_plan["receipt"]["digest"],
        "attempts": attempts,
        "secret_material_recorded": False,
        "state": "ALL_ELIGIBLE_PROVIDERS_FAILED",
    }
    failure["digest"] = sha256(failure)
    raise HTTPException(status_code=502, detail=failure)


@app.get("/deployment.json")
def deployment() -> dict[str, Any]:
    settings = load_settings()
    return {
        "schema": "szl.router-deployment/v1",
        "service": "szl-router",
        "version": APP_VERSION,
        "source_revision": source_revision(),
        "runtime_state": "MEASURED_BY_THIS_RESPONSE",
        "registry_state": settings.config_state,
        "egress_enabled": settings.egress_enabled,
        "inference": inference_readiness(settings),
        "hub_publication": "UNAVAILABLE_UNLESS_PROVIDER_READBACK_EXISTS",
    }


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")
