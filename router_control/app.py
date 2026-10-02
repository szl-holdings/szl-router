#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Sovereign-first, OpenAI-compatible routing gateway with exact receipts.

Egress is disabled by default. HTTPS providers require an exact hostname
allowlist and named environment credential; the Ollama adapter uses only a
fixed loopback endpoint and a configured model digest. Both require a validated
registry and SZL_ROUTER_ENABLE_EGRESS=1. Secrets are never returned or logged.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from router_control.verification import (
    MAX_VERIFICATION_BYTES, canonical, parse_bundle,
    sha256, verify_completion,
)

APP_VERSION = "1.1.0"
SOURCE_SCHEMA = "szl.router-source/v1"
PLAN_SCHEMA = "szl.router-plan/v1"
RECEIPT_SCHEMA = "szl.router-receipt/v1"
MAX_PROVIDERS = 16
MAX_MODELS_PER_PROVIDER = 128
MAX_MESSAGES = 64
MAX_CONTENT_CHARS = 32_000
MAX_TOTAL_CONTENT_CHARS = 96_000
MAX_RESPONSE_BYTES = 2_000_000
MAX_OLLAMA_TAGS_BYTES = 262_144
MAX_TIMEOUT_SECONDS = 45.0
MAX_ROUTE_SECONDS = 660.0
LOCAL_MODEL_CACHE_SECONDS = 2.0
OLLAMA_LOOPBACK_ORIGIN = "http://127.0.0.1:11434"
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")
OLLAMA_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$")
TOKEN_ENV = re.compile(r"^[A-Z][A-Z0-9_]{2,95}$")
MODEL_DIGEST = re.compile(r"^[0-9a-f]{64}$")
CLASSIFICATIONS = {"public", "internal", "confidential", "restricted"}
ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().parent / "static"
_local_probe_lock = threading.Lock()
_local_probe_cache: tuple[str, float, str, dict[str, Any]] | None = None


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
    provider_type: Literal["openai_https", "ollama_loopback"] = "openai_https"
    base_url: str | None = Field(default=None, min_length=9, max_length=512)
    models: dict[str, str] = Field(min_length=1, max_length=MAX_MODELS_PER_PROVIDER)
    model_digests: dict[str, str] = Field(default_factory=dict, max_length=MAX_MODELS_PER_PROVIDER)
    token_env: str | None = Field(default=None, min_length=3, max_length=96, pattern=r"^[A-Z][A-Z0-9_]{2,95}$")
    priority: int = Field(default=100, ge=0, le=10_000)
    sovereignty: int = Field(default=0, ge=0, le=100)
    cost_tier: int = Field(default=1, ge=0, le=10)
    classifications: list[str] = Field(default_factory=lambda: ["public"], min_length=1, max_length=4)
    enabled: bool = True

    @field_validator("models")
    @classmethod
    def valid_models(cls, models: dict[str, str]) -> dict[str, str]:
        for public, upstream in models.items():
            if not IDENTIFIER.fullmatch(public) or not OLLAMA_MODEL_NAME.fullmatch(upstream):
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

    @model_validator(mode="after")
    def valid_provider_binding(self) -> "ProviderRecord":
        if self.provider_type == "ollama_loopback":
            if self.base_url is not None or self.token_env is not None:
                raise ValueError("loopback Ollama endpoint and credentials are fixed by the router")
            if set(self.model_digests) != set(self.models):
                raise ValueError("each loopback model alias requires one manifest digest")
            if any(not MODEL_DIGEST.fullmatch(value) for value in self.model_digests.values()):
                raise ValueError("loopback model manifest digests must be lowercase SHA-256")
        elif (self.base_url is None or self.token_env is None or self.model_digests
              or any(not IDENTIFIER.fullmatch(upstream) for upstream in self.models.values())):
            raise ValueError("HTTPS providers require base_url and token_env without local manifest pins")
        return self


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
            normalized.append(provider if provider.provider_type == "ollama_loopback" else provider.model_copy(
                update={"base_url": validate_base_url(provider.base_url, allowed_hosts)}
            ))
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
    required_provider_id: str | None = Field(default=None, min_length=1, max_length=96,
                                             pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")
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
    required_provider_id: str | None = Field(default=None, min_length=1, max_length=96,
                                             pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")
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
        if request.required_provider_id is not None and provider.id != request.required_provider_id:
            continue
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
                "provider_type": provider.provider_type,
                "model_digest": provider.model_digests.get(request.model),
                "model_identity_state": (
                    "PIN_CONFIGURED_UNVERIFIED" if provider.provider_type == "ollama_loopback"
                    else "UNAVAILABLE_MUTABLE_MODEL_ALIAS"
                ),
                "credential_state": credential_state(provider),
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
        "required_provider_id": request.required_provider_id,
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


def credential_state(provider: ProviderRecord) -> str:
    if provider.provider_type == "ollama_loopback":
        return "NOT_REQUIRED_LOOPBACK"
    return "AVAILABLE" if provider.token_env and os.getenv(provider.token_env) else "UNAVAILABLE"


class ModelIdentityError(RuntimeError):
    """A configured local model could not satisfy its exact daemon digest pin."""

    def __init__(self, state: str):
        super().__init__(state)
        self.state = state


async def _read_ollama_inventory(kind: Literal["tags", "ps"]) -> dict[str, str]:
    """Read only the fixed local daemon; never accept a caller or registry URL."""
    timeout = httpx.Timeout(5.0, connect=2.0)
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
        limits=httpx.Limits(max_connections=2, max_keepalive_connections=0),
    ) as client:
        async with client.stream("GET", f"{OLLAMA_LOOPBACK_ORIGIN}/api/{kind}",
                                 headers={"Accept": "application/json"}) as response:
            if response.status_code != 200:
                raise ModelIdentityError("DAEMON_INVENTORY_UNAVAILABLE")
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_OLLAMA_TAGS_BYTES:
                    raise ModelIdentityError("DAEMON_INVENTORY_INVALID")
                chunks.append(chunk)
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ModelIdentityError("DAEMON_INVENTORY_INVALID")
        return result

    def reject_constant(_: str) -> None:
        raise ModelIdentityError("DAEMON_INVENTORY_INVALID")

    try:
        value = json.loads(b"".join(chunks).decode("utf-8"),
                           object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ModelIdentityError("DAEMON_INVENTORY_INVALID") from exc
    models = value.get("models") if isinstance(value, dict) else None
    if not isinstance(models, list) or len(models) > 512:
        raise ModelIdentityError("DAEMON_INVENTORY_INVALID")
    inventory: dict[str, str] = {}
    for model in models:
        if not isinstance(model, dict):
            raise ModelIdentityError("DAEMON_INVENTORY_INVALID")
        name, digest = model.get("name"), model.get("digest")
        if (not isinstance(name, str) or not OLLAMA_MODEL_NAME.fullmatch(name)
                or not isinstance(digest, str) or not MODEL_DIGEST.fullmatch(digest)
                or name in inventory):
            raise ModelIdentityError("DAEMON_INVENTORY_INVALID")
        inventory[name] = digest
    return inventory


async def ollama_inventory(kind: Literal["tags", "ps"]) -> dict[str, str]:
    return await asyncio.wait_for(_read_ollama_inventory(kind), timeout=5.0)


async def assert_ollama_identity(provider: ProviderRecord, public_model: str,
                                 *, after_completion: bool) -> dict[str, Any]:
    upstream_model = provider.models[public_model]
    expected = provider.model_digests[public_model]
    try:
        tags = await ollama_inventory("tags")
        running = await ollama_inventory("ps")
    except (httpx.HTTPError, asyncio.TimeoutError) as exc:
        raise ModelIdentityError("DAEMON_INVENTORY_UNAVAILABLE") from exc
    observed = tags.get(upstream_model)
    if observed is None:
        raise ModelIdentityError("MODEL_ABSENT")
    if observed != expected:
        raise ModelIdentityError("MODEL_DIGEST_MISMATCH")
    resident = running.get(upstream_model)
    if resident is not None and resident != expected:
        raise ModelIdentityError("RESIDENT_DIGEST_MISMATCH")
    if after_completion and resident is None:
        raise ModelIdentityError("RESIDENT_MODEL_UNAVAILABLE")
    return {
        "state": "LOCAL_DAEMON_REPORTED_MATCH",
        "basis": "OLLAMA_TAGS_AND_RUNNING_MODELS_PRE_POST",
        "expected_digest": expected,
        "observed_manifest_digest": observed,
        "observed_resident_digest": resident,
        "independent_attestation": "UNAVAILABLE",
        "license_state": "NOT_VERIFIED",
    }


async def call_provider(provider: ProviderRecord, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    token = os.getenv(provider.token_env, "") if provider.token_env else ""
    if provider.provider_type == "openai_https" and not token:
        raise RuntimeError("credential unavailable")
    base_url = (OLLAMA_LOOPBACK_ORIGIN + "/v1" if provider.provider_type == "ollama_loopback"
                else provider.base_url)
    url = base_url.rstrip("/") + "/chat/completions"
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
                **({"Authorization": f"Bearer {token}"} if token else {}),
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
            if response.status_code != 200:
                raise httpx.HTTPStatusError(
                    f"upstream status {response.status_code}",
                    request=response.request,
                    response=response,
                )
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError) as exc:
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
                "provider_type": provider.provider_type,
                "models": sorted(provider.models),
                "model_digests": provider.model_digests,
                "model_identity_state": (
                    "PIN_CONFIGURED_UNVERIFIED" if provider.provider_type == "ollama_loopback"
                    else "UNAVAILABLE_MUTABLE_MODEL_ALIAS"
                ),
                "priority": provider.priority,
                "sovereignty": provider.sovereignty,
                "cost_tier": provider.cost_tier,
                "classifications": provider.classifications,
                "enabled": provider.enabled,
                "credential_state": credential_state(provider),
                "endpoint_state": (
                    "FIXED_LOOPBACK_UNVERIFIED" if provider.provider_type == "ollama_loopback"
                    else "VALIDATED_ALLOWLISTED"
                ),
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
    remote_credentialed_provider = any(
        provider.enabled and credential_state(provider) == "AVAILABLE"
        for provider in settings.registry.providers
    )
    checks = {
        "registry_valid": settings.config_state == "VALIDATED",
        "egress_enabled": settings.egress_enabled,
        "caller_auth_configured": bool(os.getenv("SZL_ROUTER_TOKEN", "").strip()),
        "provider_configured": any(
            provider.enabled and credential_state(provider) in {"AVAILABLE", "NOT_REQUIRED_LOOPBACK"}
            for provider in settings.registry.providers
        ),
    }
    admitted = all(checks.values())
    return {
        "status": "configured" if admitted else "unavailable",
        "checks": checks,
        "remote_credentialed_provider": remote_credentialed_provider,
        "ready_for_requests": admitted,
        "basis": "LOCAL_CONFIGURATION_ONLY",
        "provider_reachability": "UNVERIFIED",
        "inference_witness": "UNAVAILABLE",
        "model_identity": "UNVERIFIED",
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
    if "szl_receipt" in value:
        raise RuntimeError("upstream completion uses a reserved receipt field")
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


async def attempt_candidate(provider: ProviderRecord, request: ChatRequest,
                            upstream_model: str) -> tuple[dict[str, Any], int, dict[str, Any] | None]:
    local_before = None
    if provider.provider_type == "ollama_loopback":
        local_before = await assert_ollama_identity(provider, request.model,
                                                    after_completion=False)
    upstream, status_code = await call_provider(provider, upstream_payload(request, upstream_model))
    validate_completion(upstream)
    if provider.provider_type != "ollama_loopback":
        return upstream, status_code, None
    if upstream.get("model") != upstream_model:
        raise ModelIdentityError("COMPLETION_MODEL_MISMATCH")
    local_after = await assert_ollama_identity(provider, request.model,
                                               after_completion=True)
    return upstream, status_code, {
        "state": "LOCAL_DAEMON_REPORTED_MATCH",
        "basis": "OLLAMA_TAGS_AND_RUNNING_MODELS_PRE_POST",
        "expected_digest": provider.model_digests[request.model],
        "pre_request_manifest_digest": local_before["observed_manifest_digest"],
        "pre_request_resident_digest": local_before["observed_resident_digest"],
        "post_request_manifest_digest": local_after["observed_manifest_digest"],
        "post_request_resident_digest": local_after["observed_resident_digest"],
        "independent_attestation": "UNAVAILABLE",
        "license_state": "NOT_VERIFIED",
    }


@app.get("/health")
@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"status": "ok", "service": "szl-router", "version": APP_VERSION}


@app.get("/version")
def version() -> dict[str, Any]:
    return {"version": APP_VERSION, "git_sha": source_revision(), "model_sha": None,
            "model_sha_state": "UNAVAILABLE_MUTABLE_MODEL_ALIASES"}


@app.get("/ready")
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
                and all(
                    provider.provider_type == "ollama_loopback" or bool(settings.allowed_hosts)
                    for provider in settings.registry.providers
                )
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


@app.get("/inference/ready")
@app.get("/readyz/inference")
def readyz_inference() -> JSONResponse:
    admission = inference_readiness(load_settings())
    return JSONResponse(status_code=200 if admission["ready_for_requests"] else 503, content=admission)


@app.get("/.well-known/szl-source.json")
@app.get("/api/source")
def source() -> dict[str, Any]:
    controlled = [
        Path(__file__),
        Path(__file__).with_name("verification.py"),
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


def local_models_body(settings: Settings, inventory_state: str,
                      tags: dict[str, str] | None = None,
                      running: dict[str, str] | None = None) -> dict[str, Any]:
    providers = [provider for provider in settings.registry.providers
                 if provider.provider_type == "ollama_loopback"]
    tags = tags or {}
    running = running or {}
    rows: list[dict[str, Any]] = []
    for provider in providers:
        provider_state = inventory_state if provider.enabled else "PROVIDER_DISABLED"
        model_rows: list[dict[str, Any]] = []
        for public_model, upstream_model in provider.models.items():
            expected = provider.model_digests[public_model]
            observed = tags.get(upstream_model) if provider_state == "REACHABLE" else None
            resident = running.get(upstream_model) if provider_state == "REACHABLE" else None
            model_rows.append({
                "public_model": public_model,
                "upstream_model": upstream_model,
                "expected_digest": expected,
                "observed_digest": observed,
                "resident_digest": resident,
                "state": (
                    "UNAVAILABLE" if provider_state != "REACHABLE" else
                    "ABSENT" if observed is None else
                    "MATCH" if observed == expected else "DIGEST_MISMATCH"
                ),
                "resident_state": (
                    "UNAVAILABLE" if provider_state != "REACHABLE" else
                    "NOT_LOADED" if resident is None else
                    "MATCH" if resident == expected else "DIGEST_MISMATCH"
                ),
                "license_state": "NOT_VERIFIED",
            })
        rows.append({
            "provider_id": provider.id,
            "provider_type": provider.provider_type,
            "inventory_state": provider_state,
            "models": model_rows,
        })
    body = {
        "schema": "szl.router-local-models/v1",
        "status": "observed" if inventory_state == "REACHABLE" else "unavailable",
        "basis": "LOCAL_DAEMON_REPORTED",
        "providers": rows,
    }
    return body


def local_models_result(body: dict[str, Any], *, observation_state: str,
                        observed_at: str | None = None,
                        observation_age_ms: float | None = None) -> dict[str, Any]:
    body = {**body, "observation_state": observation_state,
            "observed_at": observed_at, "observation_age_ms": observation_age_ms}
    return {**body, "receipt": {"algorithm": "sha256", "digest": sha256(body)}}


@app.get("/api/local-models")
async def local_models() -> dict[str, Any]:
    """Read-only, bounded daemon evidence for configured aliases only."""
    global _local_probe_cache
    settings = load_settings()
    providers = [provider for provider in settings.registry.providers
                 if provider.provider_type == "ollama_loopback"]
    if not providers:
        return local_models_result(local_models_body(settings, "UNAVAILABLE_NOT_CONFIGURED"),
                                   observation_state="NO_PROBE")
    if not settings.egress_enabled:
        return local_models_result(local_models_body(settings, "EGRESS_DISABLED"),
                                   observation_state="NO_PROBE")
    if not any(provider.enabled for provider in providers):
        return local_models_result(local_models_body(settings, "PROVIDER_DISABLED"),
                                   observation_state="NO_PROBE")
    config_key = sha256({
        "registry_state": settings.config_state,
        "egress_enabled": settings.egress_enabled,
        "allowed_hosts": sorted(settings.allowed_hosts),
        "providers": settings.registry.model_dump(mode="json"),
    })
    now = time.monotonic()
    cached = _local_probe_cache
    if cached and cached[0] == config_key and now - cached[1] < LOCAL_MODEL_CACHE_SECONDS:
        return local_models_result(cached[3], observation_state="CACHED_RECENT",
                                   observed_at=cached[2],
                                   observation_age_ms=round((now - cached[1]) * 1000, 3))
    if not _local_probe_lock.acquire(blocking=False):
        return local_models_result(local_models_body(settings, "BUSY"),
                                   observation_state="BUSY")
    try:
        # Another request may have completed between the first cache check and
        # lock acquisition; reuse its explicitly dated observation if so.
        now = time.monotonic()
        cached = _local_probe_cache
        if cached and cached[0] == config_key and now - cached[1] < LOCAL_MODEL_CACHE_SECONDS:
            return local_models_result(cached[3], observation_state="CACHED_RECENT",
                                       observed_at=cached[2],
                                       observation_age_ms=round((now - cached[1]) * 1000, 3))
        try:
            tags = await ollama_inventory("tags")
            running = await ollama_inventory("ps")
            body = local_models_body(settings, "REACHABLE", tags, running)
        except (ModelIdentityError, httpx.HTTPError, asyncio.TimeoutError):
            body = local_models_body(settings, "UNAVAILABLE")
        observed_at = datetime.now(timezone.utc).isoformat()
        completed = time.monotonic()
        _local_probe_cache = (config_key, completed, observed_at, body)
        return local_models_result(body, observation_state="LIVE_PROBE",
                                   observed_at=observed_at, observation_age_ms=0.0)
    finally:
        _local_probe_lock.release()


@app.post("/api/plan")
def route_plan(request: PlanRequest) -> dict[str, Any]:
    return plan(load_settings(), request)


@app.post("/api/verify")
async def verify(http_request: Request) -> JSONResponse:
    raw = bytearray()
    async for chunk in http_request.stream():
        if len(raw) + len(chunk) > MAX_VERIFICATION_BYTES:
            raise HTTPException(status_code=413, detail={"code": "VERIFICATION_INPUT_TOO_LARGE"})
        raw.extend(chunk)
    try:
        bundle = parse_bundle(bytes(raw))
        normalized = ChatRequest.model_validate(bundle["request"]).model_dump(mode="json")
        result = verify_completion(bundle["completion"], normalized, http_request.headers.get("X-SZL-Receipt"))
    except (ValueError, RecursionError):
        raise HTTPException(status_code=422, detail={"code": "INVALID_VERIFICATION_INPUT"}) from None
    return JSONResponse(status_code=200 if result["status"] == "CONSISTENT" else 422, content=result)


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
            required_provider_id=request.required_provider_id,
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
    deadline = started + MAX_ROUTE_SECONDS
    for candidate in candidates:
        provider = provider_by_id(settings, candidate["provider_id"])
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            attempts.append({"provider_id": provider.id, "state": "ROUTE_DEADLINE_EXCEEDED"})
            break
        if credential_state(provider) == "UNAVAILABLE":
            attempts.append({"provider_id": provider.id, "state": "SKIPPED_CREDENTIAL_UNAVAILABLE"})
            continue
        route_limited_attempt = remaining <= MAX_TIMEOUT_SECONDS
        try:
            upstream, status_code, model_identity = await asyncio.wait_for(
                attempt_candidate(provider, request, candidate["upstream_model"]),
                timeout=min(MAX_TIMEOUT_SECONDS, remaining),
            )
            elapsed_ms = round((time.monotonic() - started) * 1000, 3)
            successful_attempt = {"provider_id": provider.id, "state": "SUCCESS", "status_code": status_code}
            receipt_body = {
                "schema": RECEIPT_SCHEMA,
                "request_digest": request_digest,
                "plan_digest": request_plan["receipt"]["digest"],
                "provider_id": provider.id,
                "public_model": request.model,
                "upstream_model": candidate["upstream_model"],
                "classification": request.data_classification,
                "attempts": [*attempts, successful_attempt],
                "elapsed_ms": elapsed_ms,
                "response_digest": sha256(upstream),
                "secret_material_recorded": False,
            }
            if model_identity is not None:
                receipt_body["model_identity"] = model_identity
            receipt = {**receipt_body, "digest": sha256(receipt_body), "algorithm": "sha256"}
            result = {**upstream, "szl_receipt": receipt}
            # Apply the offline/API verifier's exact bounds to the full emitted
            # bundle before acknowledging success. Wrapper and original request
            # nodes also consume the verifier's depth, node and byte allowance.
            verification_bundle = parse_bundle(canonical({
                "completion": result,
                "request": request.model_dump(mode="json"),
            }))
            verification = verify_completion(
                verification_bundle["completion"], verification_bundle["request"],
            )
            if verification["status"] != "CONSISTENT":
                raise RuntimeError("upstream completion cannot satisfy receipt verification")
            return JSONResponse(
                status_code=200,
                content=result,
                headers={
                    "X-SZL-Receipt": receipt["digest"],
                    "Cache-Control": "no-store",
                },
            )
        except ModelIdentityError as exc:
            attempts.append({"provider_id": provider.id, "state": exc.state})
            failure = {
                "schema": RECEIPT_SCHEMA,
                "request_digest": request_digest,
                "plan_digest": request_plan["receipt"]["digest"],
                "attempts": attempts,
                "secret_material_recorded": False,
                "state": "LOCAL_MODEL_IDENTITY_UNAVAILABLE",
            }
            failure["digest"] = sha256(failure)
            raise HTTPException(status_code=503, detail=failure) from None
        except asyncio.TimeoutError:
            route_expired = route_limited_attempt or time.monotonic() >= deadline
            attempts.append({"provider_id": provider.id, "state": (
                "ROUTE_DEADLINE_EXCEEDED" if route_expired else "UPSTREAM_DEADLINE_EXCEEDED"
            )})
            if route_expired:
                break
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            attempts.append({"provider_id": provider.id, "state": "UPSTREAM_HTTP_ERROR", "status_code": status})
            if 300 <= status < 500 and status != 429:
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
