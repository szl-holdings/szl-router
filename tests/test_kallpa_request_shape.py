"""Kallpa local adversarial request-shape tests for /v1/chat/completions."""
import httpx, asyncio, pytest
from szl_router import app as app_mod

CASES = [
    ({"model": "szl-fast", "messages": "hello"}, 400),
    ({"model": "szl-fast", "messages": [1, 2]}, 400),
    ({"model": "szl-fast", "stream": True, "messages": [{"role": "user", "content": "a"}]}, 400),
    ({"model": 7, "messages": [{"role": "user", "content": "a"}]}, 400),
]

@pytest.mark.parametrize("body,want", CASES)
def test_shape_rejected_before_upstream(monkeypatch, body, want):
    called = []
    monkeypatch.setattr(app_mod.core, "chat", lambda *a, **k: called.append(1) or {})
    monkeypatch.delenv("SZL_ROUTER_TOKEN", raising=False)
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_mod.app), base_url="http://t") as c:
            return await c.post("/v1/chat/completions", json=body)
    r = asyncio.run(go())
    assert r.status_code == want
    assert not called
