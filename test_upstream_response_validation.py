"""Network-free regression tests for validated inference and embeddings failover."""

import base64
import copy
import struct

import pytest

from szl_router import core


MESSAGES = [{"role": "user", "content": "hello"}]
CHAT = {"choices": [{"message": {"role": "assistant", "content": "ready"}}]}
EMBED = {"data": [{"index": 0, "embedding": [0.25, -0.5]}]}


def test_oversized_numeric_embedding_is_a_contract_failure():
    result = {"data": [{"index": 0, "embedding": [10 ** 1000]}]}
    assert core._embedding_response_error(result, {"input": "hello"}) is not None


@pytest.fixture
def route_harness(monkeypatch):
    providers = {
        name: core.Provider(name, "", f"http://{name}.invalid/v1", "", True, "self-hosted")
        for name in ("first", "second")
    }
    routes = [(name, "test-model") for name in providers]
    monkeypatch.setattr(core, "PROVIDERS", providers)
    monkeypatch.setitem(core.MODEL_ROUTES, "test-chat", routes)
    monkeypatch.setitem(core.EMBED_ROUTES, "test-embed", routes)
    monkeypatch.setenv("SZL_COOLDOWN_SECONDS", "0")
    receipts = []
    monkeypatch.setattr(core, "_emit_route_receipt", lambda **receipt: receipts.append(receipt))
    core.embed_cache_clear()
    calls = []

    def configure(first, second, *, embeddings=False):
        def post(provider, payload, timeout):
            calls.append(provider.name)
            return copy.deepcopy(first if provider.name == "first" else second)
        monkeypatch.setattr(core, "_post_embeddings" if embeddings else "_post_chat", post)

    yield configure, calls, receipts
    core.embed_cache_clear()


@pytest.mark.parametrize("bad", [
    None, [], "choices", {},
    {"choices": None}, {"choices": []}, {"choices": {}},
    {"choices": [None]}, {"choices": [{}]},
    {"choices": [{"message": "hello"}]},
    {"choices": [{"message": {"role": "user", "content": "hello"}}]},
    {"choices": [{"message": {"role": "assistant"}}]},
    {"choices": [{"message": {"role": "assistant", "content": 42}}]},
    {"choices": [{"message": {"role": "assistant", "tool_calls": [{}]}}]},
    {"choices": [{"message": {"role": "assistant", "refusal": []}}]},
    {"choices": [{"message": {"role": "assistant", "audio": "bad"}}]},
    {**CHAT, "error": {"message": "private upstream diagnostics"}},
    {"choices": CHAT["choices"] + [None]},
])
def test_malformed_chat_fails_over_without_success_receipt(route_harness, bad):
    configure, calls, receipts = route_harness
    configure(bad, CHAT)
    response = core.chat("test-chat", MESSAGES)
    assert calls == ["first", "second"]
    provenance = response["x_szl_provenance"]
    assert provenance["provider"] == "second"
    failed, served = provenance["attempts"]
    assert failed["ok"] is False and failed["status"] == 200
    assert failed["error"].startswith("invalid chat response:")
    assert "private upstream diagnostics" not in failed["error"]
    assert served["ok"] is True
    assert len(receipts) == 1 and receipts[0]["decision"] == "served"
    assert receipts[0]["provenance"].provider == "second"


@pytest.mark.parametrize("message", [
    {"role": "assistant", "content": ""},
    {"role": "assistant", "content": "ready", "tool_calls": []},
    {"role": "assistant", "content": None},
    {"role": "assistant", "refusal": "I cannot help with this request."},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call-1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}
    ]},
    {"role": "assistant", "function_call": {"name": "lookup", "arguments": "{}"}},
    {"role": "assistant", "audio": {"id": "audio-1", "data": "AAAA", "transcript": "hello"}},
])
def test_legitimate_chat_output_variants_are_preserved(route_harness, message):
    configure, calls, _ = route_harness
    configure({"choices": [{"message": message}]}, CHAT)
    response = core.chat("test-chat", MESSAGES)
    assert calls == ["first"]
    assert response["choices"][0]["message"] == message


def test_all_malformed_chat_routes_fail_with_full_trail(route_harness):
    configure, calls, receipts = route_harness
    configure({"choices": []}, {"choices": None})
    with pytest.raises(core.RouterError) as caught:
        core.chat("test-chat", MESSAGES)
    assert calls == ["first", "second"]
    assert len(caught.value.attempts) == 2
    assert all(not attempt.ok and attempt.status == 200 for attempt in caught.value.attempts)
    assert len(receipts) == 1 and receipts[0]["decision"] == "all-routes-failed"
    assert receipts[0]["provenance"] is None


@pytest.mark.parametrize("bad", [
    None, [], "data", {}, {"data": None}, {"data": []}, {"data": {}},
    {"data": [None]}, {"data": [{}]},
    {"data": [{"index": True, "embedding": [1.0]}]},
    {"data": [{"index": -1, "embedding": [1.0]}]},
    {"data": [{"index": 1, "embedding": [1.0]}]},
    {"data": [{"index": 0, "embedding": None}]},
    {"data": [{"index": 0, "embedding": []}]},
    {"data": [{"index": 0, "embedding": [True]}]},
    {"data": [{"index": 0, "embedding": ["0.1"]}]},
    {"data": [{"index": 0, "embedding": [float("nan")]}]},
    {"data": [{"index": 0, "embedding": [float("inf")]}]},
    {**EMBED, "error": {"message": "private upstream diagnostics"}},
])
def test_malformed_embeddings_fail_over_and_only_valid_vectors_are_cached(route_harness, bad):
    configure, calls, _ = route_harness
    configure(bad, EMBED, embeddings=True)
    response = core.embed("test-embed", "hello")
    assert calls == ["first", "second"]
    assert response["data"] == EMBED["data"]
    provenance = response["x_szl_provenance"]
    assert provenance["provider"] == "second"
    assert provenance["attempts"][0]["status"] == 200
    assert provenance["attempts"][0]["error"].startswith("invalid embeddings response:")
    assert "private upstream diagnostics" not in provenance["attempts"][0]["error"]
    cached = core.embed("test-embed", "hello")
    assert calls == ["first", "second"]
    assert cached["data"] == EMBED["data"] and cached["x_szl_cache"]["hit"] is True
    assert cached["x_szl_cache"]["origin_served_by"] == "second:test-model"


@pytest.mark.parametrize("bad_rows", [
    [{"index": 0, "embedding": [1.0]}],
    [{"index": 0, "embedding": [1.0]}, {"index": 0, "embedding": [2.0]}],
    [{"index": 0, "embedding": [1.0]}, {"index": 1, "embedding": [2.0, 3.0]}],
])
def test_incomplete_or_inconsistent_embedding_batches_fail_over(route_harness, bad_rows):
    configure, calls, _ = route_harness
    valid = {"data": [{"index": 1, "embedding": [2.0]}, {"index": 0, "embedding": [1.0]}]}
    configure({"data": bad_rows}, valid, embeddings=True)
    response = core.embed("test-embed", ["a", "b"])
    assert calls == ["first", "second"]
    assert response["data"] == valid["data"]


@pytest.mark.parametrize("input_", [[101, 202], [[101, 202]]])
def test_token_inputs_are_counted_as_one_embedding(route_harness, input_):
    configure, calls, _ = route_harness
    configure(EMBED, EMBED, embeddings=True)
    assert core.embed("test-embed", input_)["data"] == EMBED["data"]
    assert calls == ["first"]


@pytest.mark.parametrize("bad_vector", [
    "not-base64!", "", "YQ==", [0.25, -0.5],
    base64.b64encode(struct.pack("<f", float("nan"))).decode(),
])
def test_base64_vectors_are_validated_without_reencoding(route_harness, bad_vector):
    configure, calls, _ = route_harness
    vector = base64.b64encode(struct.pack("<ff", 0.25, -0.5)).decode()
    valid = {"data": [{"index": 0, "embedding": vector}]}
    configure({"data": [{"index": 0, "embedding": bad_vector}]}, valid, embeddings=True)
    response = core.embed("test-embed", "hello", extra={"encoding_format": "base64"})
    assert calls == ["first", "second"]
    assert response["data"] == valid["data"]


def test_requested_embedding_dimensions_are_enforced(route_harness):
    configure, calls, _ = route_harness
    valid = {"data": [{"index": 0, "embedding": [0.25]}]}
    configure(EMBED, valid, embeddings=True)
    response = core.embed("test-embed", "hello", extra={"dimensions": 1})
    assert calls == ["first", "second"] and response["data"] == valid["data"]


def test_all_malformed_embeddings_routes_fail_and_do_not_cache(route_harness):
    configure, calls, _ = route_harness
    configure({"data": []}, {"data": None}, embeddings=True)
    for _ in range(2):
        with pytest.raises(core.RouterError) as caught:
            core.embed("test-embed", "hello")
        assert len(caught.value.attempts) == 2
        assert all(not attempt.ok and attempt.status == 200 for attempt in caught.value.attempts)
    assert calls == ["first", "second", "first", "second"]
    assert not core._EMBED_CACHE
