"""Unit tests for szl-auto complexity-aware routing (pure, no network).

score_complexity is deterministic and makes no upstream call, so these run
offline and prove the routing decision is stable and honest.
"""
from szl_router.core import score_complexity, _auto_routing_block, AUTO_MODEL


def test_short_greeting_routes_fast():
    _s, _sig, chosen = score_complexity([{"role": "user", "content": "hi there"}])
    assert chosen == "szl-fast"


def test_code_fence_routes_coder():
    prompt = "```python\ndef f():\n    return 1\n```\nwhy does this fail?"
    _s, _sig, chosen = score_complexity([{"role": "user", "content": prompt}])
    assert chosen == "szl-coder"


def test_code_keywords_route_coder():
    prompt = "fix this: import os and then const x = 1; console.log(x)"
    _s, _sig, chosen = score_complexity([{"role": "user", "content": prompt}])
    assert chosen == "szl-coder"


def test_long_reasoning_routes_large():
    prompt = ("Explain why " + ("the tradeoffs of distributed consensus matter " * 40)
              + " compare and analyze step by step in detail")
    _s, _sig, chosen = score_complexity([{"role": "user", "content": prompt}])
    assert chosen == "szl-large"


def test_deterministic():
    a = score_complexity([{"role": "user", "content": "analyze this design in detail"}])
    b = score_complexity([{"role": "user", "content": "analyze this design in detail"}])
    assert a == b


def test_score_is_bounded():
    for content in ["", "hi", "why " * 500, "```" + "x" * 5000 + "```"]:
        score, _sig, _chosen = score_complexity([{"role": "user", "content": content}])
        assert 0.0 <= score <= 1.0


def test_multimodal_content_list_does_not_crash():
    msg = [{"role": "user", "content": [
        {"type": "text", "text": "hello"},
        {"type": "image_url", "image_url": {"url": "data:x"}},
    ]}]
    _s, _sig, chosen = score_complexity(msg)
    assert chosen in ("szl-fast", "szl-large", "szl-coder")


def test_empty_messages_does_not_crash():
    _s, _sig, chosen = score_complexity([])
    assert chosen in ("szl-fast", "szl-large", "szl-coder")


def test_malformed_messages_object_does_not_crash():
    # An adversarial/malformed client can send `messages` as a JSON OBJECT
    # instead of an array. Before the fail-safe normalization this reached
    # `msgs[-1]` on a dict and raised KeyError, turning an szl-auto request into
    # an unhandled 500. It must now degrade to the cheap sovereign-first default.
    _s, _sig, chosen = score_complexity({"role": "user", "content": "hi"})
    assert chosen == "szl-fast"


def test_non_list_messages_shapes_do_not_crash():
    # Every non-list shape must resolve to a valid route, never raise.
    for bad in (None, "just a string", 123, 4.5, True, ("tuple",), {"k": "v"}, set()):
        score, _sig, chosen = score_complexity(bad)  # type: ignore[arg-type]
        assert chosen in ("szl-fast", "szl-large", "szl-coder")
        assert 0.0 <= score <= 1.0


def test_list_with_non_dict_and_none_entries_does_not_crash():
    # A list that contains non-dict junk (None / ints / strings / nested lists)
    # and no usable user turn must still route without raising.
    msgs = [None, 42, "loose text", ["nested"], {"role": "system", "content": "x"}]
    _s, _sig, chosen = score_complexity(msgs)
    assert chosen in ("szl-fast", "szl-large", "szl-coder")


def test_message_with_non_string_content_does_not_crash():
    # content that is neither str nor a list-of-parts (e.g. an int or a dict)
    # is coerced to text rather than crashing the scorer.
    for content in (123, {"unexpected": "shape"}, None, 3.14):
        _s, _sig, chosen = score_complexity([{"role": "user", "content": content}])
        assert chosen in ("szl-fast", "szl-large", "szl-coder")


def test_malformed_messages_still_deterministic():
    # Fail-safe path stays pure: identical malformed input routes identically.
    a = score_complexity({"role": "user", "content": "hi"})
    b = score_complexity({"role": "user", "content": "hi"})
    assert a == b


def test_routing_block_is_honest():
    score, signals, chosen = score_complexity([{"role": "user", "content": "hello"}])
    block = _auto_routing_block(score, signals, chosen)
    assert block["router"] == AUTO_MODEL
    assert block["chosen_logical"] == chosen
    assert "no LLM call" in block["method"]
    # Never claim optimality — it is an estimate, not a guarantee.
    assert "not a quality" in block["note"].lower()
    for word in ("optimal", "best", "guarantee"):
        assert word not in block["method"].lower()
