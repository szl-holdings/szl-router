"""Failing offline smoke checks must also fail their collected pytest tests."""
from contextlib import contextmanager
from copy import deepcopy
import importlib

import pytest


@contextmanager
def _isolated_state(monkeypatch, module, names):
    """Run fault injection on copies, then verify original state is untouched."""
    originals = {name: getattr(module, name) for name in names}
    snapshots = {name: deepcopy(value) for name, value in originals.items()
                 if isinstance(value, dict)}
    try:
        with monkeypatch.context() as state:
            for name, value in originals.items():
                state.setattr(module, name, deepcopy(value) if name in snapshots else value)
            yield
    finally:
        for name, value in originals.items():
            assert getattr(module, name) is value, f"{name} binding was not restored"
        for name, value in snapshots.items():
            assert getattr(module, name) == value, f"{name} contents were changed"


@pytest.mark.parametrize("name", [
    "cache_hit_and_miss",
    "cache_size_cap",
    "chat_not_cached",
    "pool_reuses_connection",
    "pool_drops_connection_close",
])
def test_cache_and_pool_mismatches_raise(monkeypatch, name):
    legacy = importlib.import_module("test_embed_cache_pool")
    original_ok = legacy._ok
    # Inject an actual failed check, retaining the diagnostic and failure count.
    monkeypatch.setattr(legacy, "_ok", lambda condition, message: original_ok(False, message))
    monkeypatch.delenv("SZL_RECEIPT_SINK", raising=False)
    with _isolated_state(monkeypatch, legacy.core, (
        "PROVIDERS", "MODEL_ROUTES", "EMBED_ROUTES", "_post_chat",
        "_post_embeddings", "_EMBED_CACHE", "_EMBED_CACHE_MAX", "_COOLDOWN_UNTIL",
    )):
        with pytest.raises(AssertionError, match="checks failed"):
            getattr(legacy, "test_" + name)()
        # The same mismatch stays observable to the standalone aggregate runner.
        assert getattr(legacy, "_check_" + name)() is False


def test_harvest_mismatch_raises(monkeypatch):
    legacy = importlib.import_module("test_router")
    monkeypatch.setattr(legacy.core, "_classify_harvest",
                        lambda *args: ("deliberately-wrong", False, False))
    with pytest.raises(AssertionError, match="harvest classifier checks failed"):
        legacy.test_harvest_classifier()
    assert legacy._check_harvest_classifier() is False


@pytest.mark.parametrize("name", [
    "parse_reported",
    "fetch_ok_and_unavailable",
    "sanitize_coerces",
    "unavailable_block_all_null",
    "build_body_backcompat_and_grid",
    "current_grid_context_nonblocking_and_warms",
])
def test_grid_mismatches_raise(monkeypatch, name):
    legacy = importlib.import_module("test_grid_context")
    original_check = legacy._check
    monkeypatch.setattr(legacy, "FAILED", 0)
    monkeypatch.setattr(legacy, "_check",
                        lambda condition, label: original_check(False, label))
    # A transport assertion is caught as UNAVAILABLE and may warm the cache
    # before the outer assertion raises. Preserve that state even on failure.
    with _isolated_state(monkeypatch, legacy.grid, ("_CACHE", "_REFRESH_INFLIGHT")):
        with pytest.raises(AssertionError):
            getattr(legacy, "test_" + name)()
        assert legacy.FAILED > 0
