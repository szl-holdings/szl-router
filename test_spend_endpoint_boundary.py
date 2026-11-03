"""The local operator ledger must not expose metadata before admission."""
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from szl_router import app as gateway


@pytest.fixture
def spend_state(monkeypatch):
    monkeypatch.delenv("SZL_ROUTER_TOKEN", raising=False)
    state = Mock(return_value={"tail": [{"meta": "synthetic-private-detail"}]})
    monkeypatch.setattr(gateway.spend_guard, "state", state)
    return state


@pytest.mark.parametrize("peer", ["127.0.0.1", "127.0.0.2", "::1", "::ffff:127.0.0.1"])
def test_local_operator_can_read_without_configured_token(spend_state, peer):
    response = TestClient(gateway.app, client=(peer, 12345)).get("/v1/spend")
    assert response.status_code == 200
    assert response.json() == spend_state.return_value
    spend_state.assert_called_once_with()


@pytest.mark.parametrize("peer", [
    "198.51.100.17", "::ffff:198.51.100.17", "2001:db8::1", "0.0.0.0",
    "localhost", "invalid-peer",
])
def test_non_loopback_or_unknown_peer_cannot_read(spend_state, peer):
    response = TestClient(gateway.app, client=(peer, 12345)).get(
        "/v1/spend",
        headers={"X-Forwarded-For": "127.0.0.1", "Forwarded": "for=127.0.0.1"},
    )
    assert response.status_code == 403
    assert "synthetic-private-detail" not in response.text
    spend_state.assert_not_called()


def test_missing_peer_cannot_read(spend_state):
    async def without_peer(scope, receive, send):
        await gateway.app(dict(scope, client=None), receive, send)

    response = TestClient(without_peer).get("/v1/spend")
    assert response.status_code == 403
    spend_state.assert_not_called()


@pytest.mark.parametrize("authorization", [None, "Bearer wrong-token"])
def test_configured_token_is_required_even_for_loopback(monkeypatch, spend_state, authorization):
    monkeypatch.setenv("SZL_ROUTER_TOKEN", "synthetic-operator-token")
    headers = {"Authorization": authorization} if authorization else {}
    response = TestClient(gateway.app, client=("127.0.0.1", 12345)).get(
        "/v1/spend", headers=headers,
    )
    assert response.status_code == 401
    spend_state.assert_not_called()


def test_authenticated_loopback_can_read(monkeypatch, spend_state):
    monkeypatch.setenv("SZL_ROUTER_TOKEN", "synthetic-operator-token")
    response = TestClient(gateway.app, client=("127.0.0.1", 12345)).get(
        "/v1/spend", headers={"Authorization": "Bearer synthetic-operator-token"},
    )
    assert response.status_code == 200
    assert response.json() == spend_state.return_value
    spend_state.assert_called_once_with()


def test_valid_token_does_not_remove_loopback_restriction(monkeypatch, spend_state):
    monkeypatch.setenv("SZL_ROUTER_TOKEN", "synthetic-operator-token")
    response = TestClient(gateway.app, client=("198.51.100.17", 12345)).get(
        "/v1/spend", headers={"Authorization": "Bearer synthetic-operator-token"},
    )
    assert response.status_code == 403
    spend_state.assert_not_called()
