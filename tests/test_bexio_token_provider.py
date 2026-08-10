"""Unit tests for BexioTokenProvider — cache/refresh/rotation/contention.

The token endpoint is stubbed via urlopen; KV is an in-memory fake so the
caching, locking and rotation logic is exercised without network or Redis.
"""

import json
from urllib import error as urlerror

import pytest

from api.bexio_token_provider import (BexioTokenProvider,
                                      BexioTokenRefreshContended, OAUTH_KEY)
from tests.conftest import FakeUrlopenResponse


class FakeKV:
    """Minimal in-memory stand-in for KVClient."""

    def __init__(self, blob: dict | None = None, *, lock_acquirable: bool = True):
        self.store: dict[str, str] = {}
        if blob is not None:
            self.store[OAUTH_KEY] = json.dumps(blob)
        self.lock_acquirable = lock_acquirable

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, *, ex=None):
        self.store[key] = value

    def set_nx(self, key, value, *, ex):
        if self.lock_acquirable and key not in self.store:
            self.store[key] = value
            return True
        return False

    def delete(self, key):
        self.store.pop(key, None)


@pytest.fixture
def stub_token_endpoint(monkeypatch):
    """Patch the IdP token endpoint. `state["response"]` is returned as JSON;
    set `state["error"]` to an HTTPError to simulate invalid_grant."""
    state: dict = {"response": {}, "error": None, "called": 0}

    def fake_urlopen(req, timeout=None):
        state["called"] += 1
        if state["error"] is not None:
            raise state["error"]
        return FakeUrlopenResponse(json.dumps(state["response"]).encode())

    import api.bexio_token_provider as mod
    monkeypatch.setattr(mod.urlrequest, "urlopen", fake_urlopen)
    return state


def _provider(kv, *, now=1000.0, sleep=lambda _s: None):
    return BexioTokenProvider(client_id="cid", client_secret="secret", kv=kv,
                              sleep=sleep, now=lambda: now)


def test_fresh_cached_token_skips_refresh(stub_token_endpoint):
    kv = FakeKV({"access_token": "cached", "refresh_token": "r",
                 "expires_at": 2000.0})
    token = _provider(kv, now=1000.0).get_access_token()

    assert token == "cached"
    assert stub_token_endpoint["called"] == 0  # IdP never hit


def test_expired_token_refreshes_and_rotates(stub_token_endpoint):
    stub_token_endpoint["response"] = {
        "access_token": "new_access", "refresh_token": "rotated_refresh",
        "expires_in": 3600,
    }
    kv = FakeKV({"access_token": "old", "refresh_token": "old_refresh",
                 "expires_at": 1000.0})  # expires_at == now -> stale

    token = _provider(kv, now=1000.0).get_access_token()

    assert token == "new_access"
    persisted = json.loads(kv.store[OAUTH_KEY])
    # Rotation: the NEW refresh token replaces the old one.
    assert persisted["refresh_token"] == "rotated_refresh"
    assert persisted["access_token"] == "new_access"
    assert persisted["expires_at"] == 1000.0 + 3600
    # Lock released.
    assert "bexio:refresh_lock" not in kv.store


def test_refresh_falls_back_to_previous_refresh_token_if_omitted(stub_token_endpoint):
    stub_token_endpoint["response"] = {"access_token": "a", "expires_in": 3600}
    kv = FakeKV({"access_token": "old", "refresh_token": "keepme",
                 "expires_at": 0.0})

    _provider(kv).get_access_token()

    assert json.loads(kv.store[OAUTH_KEY])["refresh_token"] == "keepme"


def test_missing_refresh_token_raises(stub_token_endpoint):
    kv = FakeKV()  # empty store — nothing bootstrapped
    with pytest.raises(RuntimeError, match="bexio_oauth_bootstrap"):
        _provider(kv).get_access_token()


def test_invalid_grant_propagates_as_httperror(stub_token_endpoint):
    stub_token_endpoint["error"] = urlerror.HTTPError(
        "url", 400, "invalid_grant", {}, fp=None)
    kv = FakeKV({"access_token": "old", "refresh_token": "dead",
                 "expires_at": 0.0})

    with pytest.raises(urlerror.HTTPError):
        _provider(kv).get_access_token()


def test_contention_returns_winner_token(stub_token_endpoint):
    """Lock held by another instance; the winner publishes a token mid-wait."""
    kv = FakeKV({"access_token": "old", "refresh_token": "r",
                 "expires_at": 0.0}, lock_acquirable=False)

    def winner_publishes(_s):
        kv.store[OAUTH_KEY] = json.dumps(
            {"access_token": "winner", "refresh_token": "r2",
             "expires_at": 9_999_999_999})

    token = _provider(kv, now=1000.0, sleep=winner_publishes).get_access_token()
    assert token == "winner"
    assert stub_token_endpoint["called"] == 0  # we never refreshed ourselves


def test_contention_gives_up_as_transient(stub_token_endpoint):
    """Lock held and no token ever published -> transient error (maps to 502)."""
    kv = FakeKV({"access_token": "old", "refresh_token": "r",
                 "expires_at": 0.0}, lock_acquirable=False)

    provider = _provider(kv, now=1000.0)
    with pytest.raises(BexioTokenRefreshContended):
        provider.get_access_token()
    # It's a URLError subclass so the endpoint's existing arm maps it to 502.
    assert isinstance(BexioTokenRefreshContended(), urlerror.URLError)
