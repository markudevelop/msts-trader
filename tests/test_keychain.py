"""Keychain tests — use an in-memory backend so no real OS keychain is touched."""
from __future__ import annotations

import keyring
import pytest
from keyring.backend import KeyringBackend


class _MemBackend(KeyringBackend):
    priority = 100

    def __init__(self):
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self._store.get((service, username))

    def set_password(self, service, username, password):
        self._store[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self._store:
            raise keyring.errors.PasswordDeleteError("not set")
        del self._store[(service, username)]


@pytest.fixture(autouse=True)
def mem_keyring(monkeypatch):
    backend = _MemBackend()
    monkeypatch.setattr(keyring, "get_keyring", lambda: backend)
    monkeypatch.setattr(keyring, "get_password", lambda s, u: backend.get_password(s, u))
    monkeypatch.setattr(keyring, "set_password", lambda s, u, p: backend.set_password(s, u, p))
    monkeypatch.setattr(keyring, "delete_password", lambda s, u: backend.delete_password(s, u))
    yield backend


def test_save_then_load_roundtrip():
    from msts_trader import keychain

    keychain.save("tastytrade", {"provider_secret": "x", "refresh_token": "y", "account_id": "abc"})
    out = keychain.load("tastytrade")
    assert out == {"provider_secret": "x", "refresh_token": "y", "account_id": "abc"}


def test_load_missing_raises():
    from msts_trader import keychain

    with pytest.raises(keychain.CredsMissingError):
        keychain.load("alpaca")


def test_clear_removes_creds():
    from msts_trader import keychain

    keychain.save("alpaca", {"api_key": "a", "secret_key": "b", "paper": True})
    keychain.clear("alpaca")
    with pytest.raises(keychain.CredsMissingError):
        keychain.load("alpaca")


def test_secret_roundtrip_and_clear():
    from msts_trader import keychain

    keychain.save_secret("schwab_oauth_token", '{"token": "x"}')
    assert keychain.load_secret("schwab_oauth_token") == '{"token": "x"}'
    keychain.clear_secret("schwab_oauth_token")
    assert keychain.load_secret("schwab_oauth_token") is None


def test_default_broker_roundtrip():
    from msts_trader import keychain

    assert keychain.get_default() is None
    keychain.set_default("tastytrade")
    assert keychain.get_default() == "tastytrade"
    keychain.clear_default()
    assert keychain.get_default() is None


def test_list_brokers_returns_configured_only():
    from msts_trader import keychain

    assert keychain.list_brokers() == []
    keychain.save("paper", {"starting_cash": "100000"})
    keychain.save("tastytrade", {"provider_secret": "x", "refresh_token": "y", "account_id": ""})
    out = sorted(keychain.list_brokers())
    assert out == ["paper", "tastytrade"]
