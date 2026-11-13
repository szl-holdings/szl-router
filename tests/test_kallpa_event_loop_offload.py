"""Regression: a slow upstream must not serialize requests or block /healthz.

Kallpa local test (not upstream). Uses an in-process stub for core.chat that
sleeps, then drives the ASGI app concurrently via httpx.ASGITransport.
"""
import asyncio
import time

import httpx
import pytest

from szl_router import app as app_mod


@pytest.mark.parametrize("n", [4])
def test_concurrent_chats_overlap_and_healthz_stays_fast(monkeypatch, n):
    delay = 0.5

    def slow_chat(model, messages, **kw):
        time.sleep(delay)
        return {"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                "x_szl_provenance": {}}

    monkeypatch.setattr(app_mod.core, "chat", slow_chat)
    monkeypatch.setattr(app_mod.receipts, "build_envelope", lambda **kw: None)
    monkeypatch.delenv("SZL_ROUTER_TOKEN", raising=False)

    async def run():
        transport = httpx.ASGITransport(app=app_mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            body = {"model": "szl-fast", "messages": [{"role": "user", "content": "hi"}]}

            async def health():
                await asyncio.sleep(0.1)
                t = time.perf_counter()
                r = await c.get("/healthz")
                return r.status_code, time.perf_counter() - t

            t0 = time.perf_counter()
            res = await asyncio.gather(*[c.post("/v1/chat/completions", json=body) for _ in range(n)], health())
            return time.perf_counter() - t0, res

    total, res = asyncio.run(run())
    assert all(r.status_code == 200 for r in res[:n])
    hz_status, hz_latency = res[n]
    assert hz_status == 200
    assert hz_latency < delay, f"/healthz blocked for {hz_latency:.2f}s"
    assert total < delay * n * 0.75, f"requests serialized: {total:.2f}s for {n}x{delay}s"
