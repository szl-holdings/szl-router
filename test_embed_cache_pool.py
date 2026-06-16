"""Offline, deterministic tests for the exact-hash embeddings cache and the
upstream keep-alive connection pool. No network: the upstream POST is stubbed.

Run: python3 test_embed_cache_pool.py   (exits non-zero on any failure)

Doctrine guards: a cache hit returns BYTE-IDENTICAL vectors, is labelled
served_by ...:cache HONESTLY, never fabricates, and never flips sovereign. The
chat path is NOT cached. The pool reuses connections without changing behavior.
"""
import os
import sys

sys.path.insert(0, "szl_router")
import core  # noqa: E402


# --- a fake armed sovereign provider, with a counting upstream stub -----------
class _Counter:
    def __init__(self):
        self.calls = 0


def _arm_fake_embed_provider(counter, vector=(0.1, 0.2, 0.3)):
    """Point the bge-large route at a single fake provider whose upstream POST is
    counted, so a cache HIT is observable as 'no new upstream call'."""
    fake = core.Provider(
        name="omen_gpu",  # reuse a real sovereign slot so tier/sovereign are honest
        base_url_env="",
        base_url_default="http://fake-node.local/v1",
        key_env="",
        sovereign=True,
        energy_source="self-hosted",
    )
    core.PROVIDERS["omen_gpu"] = fake
    core.EMBED_ROUTES["bge-large"] = [("omen_gpu", "bge-large")]

    def fake_post(provider, payload, timeout):
        counter.calls += 1
        return {
            "object": "list",
            "data": [{"object": "embedding", "index": 0, "embedding": list(vector)}],
            "model": payload["model"],
        }

    core._post_embeddings = fake_post


def _ok(cond, msg):
    print(("  OK  " if cond else "  BAD ") + msg)
    return 0 if cond else 1


def test_cache_hit_and_miss():
    print("== embeddings exact-hash cache: hit + miss ==")
    failed = 0
    core.embed_cache_clear()
    counter = _Counter()
    _arm_fake_embed_provider(counter)

    # 1st call: miss -> hits upstream once, fresh result (no cache marker).
    r1 = core.embed("bge-large", "hello world")
    failed += _ok(counter.calls == 1, "first identical request is a MISS (1 upstream call)")
    failed += _ok("x_szl_cache" not in r1, "fresh result carries NO cache marker")
    failed += _ok(r1["x_szl_provenance"]["served_by"] == "omen_gpu:bge-large",
                  "fresh served_by is the real upstream")

    # 2nd identical call: HIT -> NO new upstream call, byte-identical vectors.
    r2 = core.embed("bge-large", "hello world")
    failed += _ok(counter.calls == 1, "second identical request is a HIT (still 1 upstream call)")
    failed += _ok(r2["data"][0]["embedding"] == r1["data"][0]["embedding"],
                  "cache hit returns BYTE-IDENTICAL vectors")
    failed += _ok(r2.get("x_szl_cache", {}).get("served_by") == "cache",
                  "cache hit marked served_by:cache honestly")
    failed += _ok(r2["x_szl_provenance"]["served_by"] == "omen_gpu:bge-large:cache",
                  "provenance served_by gains honest ':cache' suffix")
    failed += _ok(r2["x_szl_provenance"]["sovereign"] is True,
                  "cache hit preserves the honest sovereign label (not fabricated)")

    # different input: MISS -> fresh upstream call.
    r3 = core.embed("bge-large", "different input")
    failed += _ok(counter.calls == 2, "non-identical request is a MISS (fresh upstream call)")
    failed += _ok("x_szl_cache" not in r3, "non-identical fresh result has no cache marker")

    # extra params participate in the key: same input, different extra -> MISS.
    core.embed("bge-large", "hello world", extra={"encoding_format": "float"})
    failed += _ok(counter.calls == 3, "differing extra params bypass the cache (MISS)")

    # use_cache=False forces a fresh call even for a cached key.
    core.embed("bge-large", "hello world", use_cache=False)
    failed += _ok(counter.calls == 4, "use_cache=False forces a fresh upstream call")

    # mutating a returned hit must not poison the stored entry.
    r_hit = core.embed("bge-large", "hello world")
    r_hit["data"][0]["embedding"][0] = 999.0
    r_hit2 = core.embed("bge-large", "hello world")
    failed += _ok(r_hit2["data"][0]["embedding"][0] != 999.0,
                  "mutating a returned hit does NOT corrupt the cache")
    print()
    return failed == 0


def test_cache_size_cap():
    print("== embeddings cache: size cap evicts oldest ==")
    failed = 0
    core.embed_cache_clear()
    counter = _Counter()
    _arm_fake_embed_provider(counter)
    orig_max = core._EMBED_CACHE_MAX
    core._EMBED_CACHE_MAX = 2
    try:
        core.embed("bge-large", "a")   # call 1
        core.embed("bge-large", "b")   # call 2
        core.embed("bge-large", "c")   # call 3 -> evicts oldest ("a")
        failed += _ok(counter.calls == 3, "three distinct inputs => three upstream calls")
        core.embed("bge-large", "a")   # "a" was evicted -> MISS, call 4
        failed += _ok(counter.calls == 4, "evicted entry re-fetches (cap enforced)")
        core.embed("bge-large", "c")   # "c" still cached -> HIT, no new call
        failed += _ok(counter.calls == 4, "newest entry still cached (HIT)")
    finally:
        core._EMBED_CACHE_MAX = orig_max
    print()
    return failed == 0


def test_chat_not_cached():
    print("== chat path is NOT cached (correctness-sensitive) ==")
    failed = 0
    counter = _Counter()

    fake = core.Provider(
        name="omen_gpu", base_url_env="", base_url_default="http://fake-node.local/v1",
        key_env="", sovereign=True, energy_source="self-hosted",
    )
    core.PROVIDERS["omen_gpu"] = fake
    core.MODEL_ROUTES["szl-fast"] = [("omen_gpu", "llama3.1:8b")]

    def fake_chat(provider, payload, timeout):
        counter.calls += 1
        return {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}

    core._post_chat = fake_chat
    core.chat("szl-fast", [{"role": "user", "content": "same"}])
    core.chat("szl-fast", [{"role": "user", "content": "same"}])
    failed += _ok(counter.calls == 2, "two identical chats => two upstream calls (no chat cache)")
    print()
    return failed == 0


def test_pool_reuses_connection():
    print("== connection pool reuses keep-alive connections ==")
    failed = 0

    class FakeResp:
        version = 11
        status = 200
        reason = "OK"

        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body

        def getheader(self, name, default=None):
            return default

        def getheaders(self):
            return []

    class FakeConn:
        created = 0
        requests = 0

        def __init__(self, *a, **k):
            FakeConn.created += 1
            self.timeout = k.get("timeout")

        def request(self, method, path, body=None, headers=None):
            FakeConn.requests += 1

        def getresponse(self):
            return FakeResp(b'{"data": [{"embedding": [1.0]}]}')

        def close(self):
            pass

    pool = core._ConnectionPool()
    pool._new_conn = lambda scheme, host, port, timeout: FakeConn(timeout=timeout)

    url = "http://fake-node.local/v1/embeddings"
    hdrs = {"Content-Type": "application/json"}
    pool.request_json(url, b"{}", hdrs, 5.0)
    pool.request_json(url, b"{}", hdrs, 5.0)
    pool.request_json(url, b"{}", hdrs, 5.0)
    failed += _ok(FakeConn.created == 1, "3 requests to same host => 1 connection created (reused)")
    failed += _ok(FakeConn.requests == 3, "all 3 requests went out over the pooled connection")
    print()
    return failed == 0


def test_pool_drops_connection_close():
    print("== pool honors Connection: close (drops, does not reuse) ==")
    failed = 0

    class FakeResp:
        version = 11
        status = 200
        reason = "OK"

        def read(self):
            return b'{"data": [{"embedding": [1.0]}]}'

        def getheader(self, name, default=None):
            return "close" if name.lower() == "connection" else default

        def getheaders(self):
            return [("Connection", "close")]

    class FakeConn:
        created = 0
        closed = 0

        def __init__(self, *a, **k):
            FakeConn.created += 1

        def request(self, *a, **k):
            pass

        def getresponse(self):
            return FakeResp()

        def close(self):
            FakeConn.closed += 1

    pool = core._ConnectionPool()
    pool._new_conn = lambda scheme, host, port, timeout: FakeConn()
    url = "http://fake-node.local/v1/embeddings"
    pool.request_json(url, b"{}", {}, 5.0)
    pool.request_json(url, b"{}", {}, 5.0)
    failed += _ok(FakeConn.created == 2, "Connection: close => fresh conn each call (no reuse)")
    print()
    return failed == 0


if __name__ == "__main__":
    results = [
        test_cache_hit_and_miss(),
        test_cache_size_cap(),
        test_chat_not_cached(),
        test_pool_reuses_connection(),
        test_pool_drops_connection_close(),
    ]
    if all(results):
        print("RESULT: embeddings cache + connection pool tests PASSED.")
        sys.exit(0)
    print("RESULT: FAILURES above.")
    sys.exit(1)
