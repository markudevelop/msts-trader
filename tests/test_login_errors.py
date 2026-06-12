from __future__ import annotations

from msts_trader.login_errors import explain_login_error


def test_tastytrade_grant_revoked():
    e = Exception("Couldn't parse response: {'error_code': 'invalid_grant', 'error_description': 'Grant revoked'}")
    msg = explain_login_error("tastytrade", e)
    assert "revoked or has expired" in msg
    assert "developer.tastytrade.com" in msg


def test_tastytrade_invalid_client():
    msg = explain_login_error("tastytrade", Exception("invalid_client"))
    assert "provider secret" in msg.lower()


def test_alpaca_unauthorized():
    msg = explain_login_error("alpaca", Exception("403 Forbidden"))
    assert "api key" in msg.lower()


def test_ibkr_connection_refused():
    msg = explain_login_error("ibkr", Exception("connection refused at 127.0.0.1:4002"))
    assert "tws" in msg.lower() or "gateway" in msg.lower()


def test_schwab_token_expired():
    msg = explain_login_error("schwab", Exception("token expired"))
    assert "--reauth" in msg
    # token-expiry errors can also be a first-login callback mismatch — the
    # message must point at the exact-match requirement
    assert "EXACTLY" in msg


def test_schwab_callback_mismatch():
    msg = explain_login_error("schwab", Exception("invalid_request: redirect_uri mismatch"))
    assert "callback" in msg.lower()
    assert "trailing slash" in msg.lower()


def test_unknown_falls_back_to_raw():
    msg = explain_login_error("tastytrade", Exception("some totally novel error"))
    assert "some totally novel error" in msg
