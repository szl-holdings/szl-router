# SPDX-License-Identifier: Apache-2.0
"""Strict, text-only adapters for two non-Chat-Completions APIs.

Pure transformations only: no network, keys, tools, model qualification, or
claim that a provider is licensed, billed, signed, or ready for deployment.
Unsupported request/response semantics fail closed instead of being discarded.
"""
from __future__ import annotations

from typing import Any

ANTHROPIC_API_VERSION = "2026-10-21"
MAX_BRIDGED_OUTPUT_TOKENS = 4096
MAX_BRIDGED_MESSAGES = 64

def _bounded_request(payload: dict[str, Any]) -> tuple[str, int, list[dict[str, Any]]]:
    model = payload.get("model")
    tokens = payload.get("max_tokens")
    messages = payload.get("messages")
    if not isinstance(model, str) or not model or len(model) > 192:
        raise ValueError("bridged upstream model is invalid")
    if type(tokens) is not int or not 1 <= tokens <= MAX_BRIDGED_OUTPUT_TOKENS:
        raise ValueError("bridged providers require a bounded max_tokens")
    if (not isinstance(messages, list) or not 1 <= len(messages) <= MAX_BRIDGED_MESSAGES):
        raise ValueError("bridged providers require bounded messages")
    if payload.get("stream") is not False or payload.get("user") is not None:
        raise ValueError("bridged providers refuse streaming and user metadata")
    if payload.get("temperature") is not None and payload.get("top_p") is not None:
        raise ValueError("bridged providers refuse competing sampling settings")
    return model, tokens, messages


def _plain_messages(messages: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, str]]]:
    system: list[str] = []
    turns: list[dict[str, str]] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("name") is not None:
            raise ValueError("message metadata is not supported by the bridge")
        role, content = message.get("role"), message.get("content")
        if not isinstance(content, str) or not content:
            raise ValueError("only nonempty text messages are supported")
        if role == "system":
            if turns:
                raise ValueError("interleaved system messages cannot be preserved")
            system.append(content)
        elif role in {"user", "assistant"}:
            turns.append({"role": role, "content": content})
        else:
            raise ValueError("tool or unknown roles require a separate reviewed adapter")
    if not turns or turns[-1]["role"] != "user":
        raise ValueError("text bridge requires a final user turn")
    return "\n\n".join(system) if system else None, turns


def prepare_provider_request(
    provider_type: str, payload: dict[str, Any]
) -> tuple[str, dict[str, Any], dict[str, str]]:
    """Return fixed route, request JSON and non-secret headers."""
    if provider_type in {"openai_https", "ollama_loopback"}:
        return "/chat/completions", payload, {}
    if provider_type not in {"anthropic_messages_https", "perplexity_agent_https"}:
        raise ValueError("unsupported provider protocol")
    model, tokens, messages = _bounded_request(payload)
    system, turns = _plain_messages(messages)
    if provider_type == "anthropic_messages_https":
        out: dict[str, Any] = {
            "model": model,
            "max_tokens": tokens,
            "messages": turns,
            "stream": False,
        }
        if system is not None:
            out["system"] = system
        for key in ("temperature", "top_p"):
            if payload.get(key) is not None:
                out[key] = payload[key]
        stop = payload.get("stop")
        if stop is not None:
            out["stop_sequences"] = [stop] if isinstance(stop, str) else stop
        return "/v1/messages", out, {"anthropic-version": ANTHROPIC_API_VERSION}

    # Perplexity Agent API accepts Open Responses, not retired Sonar completions.
    # Restrict to one user turn: no implicit multi-turn conversion or web tools.
    if len(turns) != 1 or turns[0]["role"] != "user" or payload.get("stop") is not None:
        raise ValueError("Perplexity bridge is single-turn only and has no stop mapping")
    out = {
        "model": model,
        "input": turns[0]["content"],
        "max_output_tokens": tokens,
        "stream": False,
        "store": False,
        "tools": [],
    }
    if system is not None:
        out["instructions"] = system
    for key in ("temperature", "top_p"):
        if payload.get(key) is not None:
            out[key] = payload[key]
    return "/v1/agent", out, {}


def _text_only(blocks: Any, expected: str) -> str:
    if not isinstance(blocks, list) or not 1 <= len(blocks) <= 64:
        raise ValueError("provider text blocks are absent or unbounded")
    pieces: list[str] = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != expected:
            raise ValueError("non-text or tool output cannot be silently normalized")
        if expected == "output_text" and block.get("annotations"):
            raise ValueError("annotated citations require a provenance-preserving adapter")
        if expected == "text" and "citations" in block:
            citations = block["citations"]
            if citations is not None and (not isinstance(citations, list) or citations):
                raise ValueError("citations require a provenance-preserving adapter")
        text = block.get("text")
        if not isinstance(text, str):
            raise ValueError("provider text block is malformed")
        pieces.append(text)
    return "\n".join(pieces)


def _usage(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ValueError("provider usage absent; accounting cannot be normalized")
    a, b = value.get("input_tokens"), value.get("output_tokens")
    if type(a) is not int or type(b) is not int or a < 0 or b < 0:
        raise ValueError("provider token usage is invalid")
    return {"prompt_tokens": a, "completion_tokens": b, "total_tokens": a + b}


def normalize_provider_response(provider_type: str, value: dict[str, Any]) -> dict[str, Any]:
    """Normalize only witnessed complete text responses, never tool invocations."""
    if provider_type in {"openai_https", "ollama_loopback"}:
        return value
    if not isinstance(value, dict):
        raise ValueError("provider response must be an object")
    ident, model = value.get("id"), value.get("model")
    if any(not isinstance(x, str) or not x or len(x) > 256 for x in (ident, model)):
        raise ValueError("provider response identity is invalid")

    if provider_type == "anthropic_messages_https":
        if value.get("type") != "message" or value.get("role") != "assistant":
            raise ValueError("Anthropic response is not an assistant message")
        stop = value.get("stop_reason")
        if stop not in {"end_turn", "stop_sequence", "max_tokens"}:
            raise ValueError("Anthropic response has an unsupported stop reason")
        content = _text_only(value.get("content"), "text")
        finish = "length" if stop == "max_tokens" else "stop"
    elif provider_type == "perplexity_agent_https":
        if (value.get("object") != "response" or value.get("status") != "completed"
                or value.get("error") is not None
                or value.get("incomplete_details") is not None):
            raise ValueError("Perplexity Agent response was not completed")
        items = value.get("output")
        if not isinstance(items, list) or len(items) != 1:
            raise ValueError("Perplexity tool/search/reasoning outputs need reviewed preservation")
        item = items[0]
        if (not isinstance(item, dict) or item.get("type") != "message"
                or item.get("role") != "assistant" or item.get("status") != "completed"):
            raise ValueError("Perplexity returned unsupported output items")
        content = _text_only(item.get("content"), "output_text")
        finish = "stop"
    else:
        raise ValueError("unsupported provider protocol")
    return {
        "id": ident,
        "object": "chat.completion",
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": finish,
        }],
        "usage": _usage(value.get("usage")),
    }
