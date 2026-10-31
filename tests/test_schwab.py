"""Schwab adapter parsing — mock the schwab-py client (no network)."""

from __future__ import annotations

import json
import pytest

pytest.importorskip("schwab")  # optional dep — skip when the SDK is not installed

from decimal import Decimal
from types import SimpleNamespace

import msts_trader.brokers.schwab as schwab_mod
from msts_trader.brokers.schwab import Schwab


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _broker(account=None, quotes=None):
    b = Schwab.__new__(Schwab)
    b._account_hash = "HASH"
    b.account_hash = "HASH"
    b.account_id = "HASH…"
    b._client = SimpleNamespace(
        get_account=lambda h, fields=None: _Resp(account or {}),
        get_quotes=lambda syms: _Resp(quotes or {}),
    )
    return b


def test_balances():
    acct = {
        "securitiesAccount": {
            "currentBalances": {
                "liquidationValue": 50000,
                "cashBalance": 10000,
                "buyingPower": 80000,
            }
        }
    }
    bal = _broker(account=acct).balances()
    assert bal.nav == Decimal("50000")
    assert bal.cash == Decimal("10000")
    assert bal.buying_power == Decimal("80000")


def test_positions_filters_non_equity_and_computes_price():
    acct = {
        "securitiesAccount": {
            "positions": [
                {"instrument": {"symbol": "SPY", "assetType": "ETF"}, "longQuantity": 10, "marketValue": 5000},
                {"instrument": {"symbol": "AAPL", "assetType": "EQUITY"}, "longQuantity": 5, "marketValue": 1000},
                {
                    "instrument": {"symbol": "OPT", "assetType": "OPTION"},
                    "longQuantity": 1,
                    "marketValue": 100,
                },  # skipped
                {"instrument": {"symbol": "ZERO", "assetType": "EQUITY"}, "longQuantity": 0, "marketValue": 0},  # flat
            ]
        }
    }
    out = _broker(account=acct).positions()
    assert set(out) == {"SPY", "AAPL"}
    assert out["SPY"].quantity == Decimal("10")
    assert out["SPY"].price == Decimal("500")  # 5000 / 10


def test_positions_short_quantity():
    acct = {
        "securitiesAccount": {
            "positions": [
                {"instrument": {"symbol": "TSLA", "assetType": "EQUITY"}, "shortQuantity": 4, "marketValue": -1600},
            ]
        }
    }
    out = _broker(account=acct).positions()
    assert out["TSLA"].quantity == Decimal("-4")


def test_quote_price_priority():
    quotes = {
        "SPY": {"quote": {"lastPrice": 500.1}},
        "QQQ": {"quote": {"mark": 400.2, "closePrice": 399}},  # no last -> mark
        "IWM": {"quote": {"closePrice": 200.0}},  # only close
        "DEAD": {"quote": {"lastPrice": 0}},  # zero -> dropped
    }
    out = _broker(quotes=quotes).quote(["SPY", "QQQ", "IWM", "DEAD"])
    assert out["SPY"] == Decimal("500.1")
    assert out["QQQ"] == Decimal("400.2")
    assert out["IWM"] == Decimal("200.0")
    assert "DEAD" not in out


def test_quote_empty():
    assert _broker().quote([]) == {}


def test_legacy_token_file_migrates_to_keychain(tmp_path, monkeypatch):
    store = {}

    monkeypatch.setattr(schwab_mod.keychain, "load_secret", lambda name: store.get(name))
    monkeypatch.setattr(schwab_mod.keychain, "save_secret", lambda name, value: store.__setitem__(name, value))
    legacy = tmp_path / "schwab_token.json"
    payload = {"creation_timestamp": 123, "token": {"access_token": "A", "refresh_token": "R"}}
    legacy.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(schwab_mod, "TOKEN_PATH", legacy)

    assert schwab_mod._load_stored_token() == payload
    assert json.loads(store[schwab_mod.TOKEN_KEY]) == payload
    assert not legacy.exists()


def test_schwab_init_loads_keychain_token_without_persistent_file(tmp_path, monkeypatch):
    store = {
        schwab_mod.TOKEN_KEY: json.dumps(
            {"creation_timestamp": 123, "token": {"access_token": "A", "refresh_token": "R"}}
        )
    }
    captured = {}

    monkeypatch.setattr(schwab_mod.keychain, "load_secret", lambda name: store.get(name))
    monkeypatch.setattr(schwab_mod.keychain, "save_secret", lambda name, value: store.__setitem__(name, value))
    monkeypatch.setattr(schwab_mod, "TOKEN_PATH", tmp_path / "schwab_token.json")

    class _Client:
        def token_age(self):
            return 0

        def get_account_numbers(self):
            return _Resp([{"hashValue": "HASH123456"}])

    def fake_client_from_access_functions(api_key, app_secret, token_read_func, token_write_func, enforce_enums=True):
        captured["api_key"] = api_key
        captured["app_secret"] = app_secret
        captured["token"] = token_read_func()
        captured["enforce_enums"] = enforce_enums
        token_write_func({"creation_timestamp": 123, "token": {"access_token": "NEW", "refresh_token": "R"}})
        return _Client()

    def fake_login_flow(**kwargs):
        raise AssertionError("stored keychain token should avoid browser login")

    monkeypatch.setattr(schwab_mod, "client_from_access_functions", fake_client_from_access_functions)
    monkeypatch.setattr(schwab_mod, "client_from_login_flow", fake_login_flow)

    b = Schwab("app-key", "app-secret")
    assert b.account_hash == "HASH123456"
    assert captured["api_key"] == "app-key"
    assert captured["app_secret"] == "app-secret"
    assert captured["token"]["token"]["access_token"] == "A"
    assert captured["enforce_enums"] is False
    assert json.loads(store[schwab_mod.TOKEN_KEY])["token"]["access_token"] == "NEW"
    assert not schwab_mod.TOKEN_PATH.exists()


# ----- place_market -----


class _OrderResp:
    def __init__(self, location=None):
        self.headers = {"Location": location} if location else {}

    def raise_for_status(self):
        return None


def test_place_market_submits_and_reads_location(monkeypatch):
    from msts_trader.models import Order, Side
    from decimal import Decimal as D

    b = _broker()
    b._client = SimpleNamespace(
        place_order=lambda h, spec: _OrderResp("https://api.schwab.com/v1/accounts/HASH/orders/12345")
    )
    r = b.place_market(Order(ticker="SPY", side=Side.BUY, quantity=D("10")))
    assert r["status"] == "submitted" and r["order_id"] == "12345" and r["quantity"] == 10


def test_place_market_dry_run():
    from msts_trader.models import Order, Side
    from decimal import Decimal as D

    r = _broker().place_market(Order(ticker="SPY", side=Side.SELL, quantity=D("5")), dry_run=True)
    assert r["status"] == "dry-run" and r["dry_run"] is True


def test_place_market_sub_share_is_sent_as_fractional_quantity():
    from msts_trader.models import Order, Side
    from decimal import Decimal as D

    captured = {}
    b = _broker()
    b._client = SimpleNamespace(
        place_order=lambda h, spec: (
            captured.update(spec=spec) or _OrderResp("https://api.schwab.com/v1/accounts/HASH/orders/12346")
        )
    )
    r = b.place_market(Order(ticker="SPY", side=Side.BUY, quantity=D("0.4"), estimated_price=D("10")))
    assert r["status"] == "submitted" and r["quantity"] == 0.4
    assert captured["spec"]["orderLegCollection"][0]["quantity"] == 0.4


def test_place_market_fractional_quantity_is_rounded_down_to_four_places():
    from msts_trader.models import Order, Side
    from decimal import Decimal as D

    captured = {}
    b = _broker()
    b._client = SimpleNamespace(
        place_order=lambda h, spec: (
            captured.update(spec=spec) or _OrderResp("https://api.schwab.com/v1/accounts/HASH/orders/12347")
        )
    )
    r = b.place_market(Order(ticker="SPY", side=Side.BUY, quantity=D("0.123456"), estimated_price=D("100")))
    assert r["status"] == "submitted" and r["quantity"] == 0.1234
    assert captured["spec"]["orderLegCollection"][0]["quantity"] == 0.1234


def test_place_market_skips_fractional_order_below_one_dollar():
    from msts_trader.models import Order, Side
    from decimal import Decimal as D

    r = _broker().place_market(
        Order(ticker="SPY", side=Side.BUY, quantity=D("0.1"), estimated_price=D("9.99"))
    )
    assert r["status"] == "skipped" and "$1" in r["reason"]


def test_place_market_skips_fractional_moc_order():
    from msts_trader.models import Order, Side
    from decimal import Decimal as D

    r = _broker().place_market(Order(ticker="SPY", side=Side.BUY, quantity=D("0.5"), moc=True))
    assert r["status"] == "skipped" and "regular-session only" in r["reason"]


def test_place_limit_fractional_share_order_is_sent():
    from msts_trader.models import Order, Side
    from decimal import Decimal as D

    captured = {}
    b = _broker()
    b._client = SimpleNamespace(
        place_order=lambda h, spec: (
            captured.update(spec=spec) or _OrderResp("https://api.schwab.com/v1/accounts/HASH/orders/12348")
        )
    )
    r = b.place_limit(Order(ticker="SPY", side=Side.BUY, quantity=D("1.25")), D("100"))
    assert r["status"] == "submitted" and r["quantity"] == 1.25
    assert captured["spec"]["orderLegCollection"][0]["quantity"] == 1.25


def test_place_limit_sub_share_order_skips_for_market_fallback():
    from msts_trader.models import Order, Side
    from decimal import Decimal as D

    r = _broker().place_limit(Order(ticker="SPY", side=Side.SELL, quantity=D("0.5")), D("100"))
    assert r["status"] == "skipped" and "must use market execution" in r["reason"]


def test_place_market_moc_sets_order_type(monkeypatch):
    # order.moc must produce a MARKET_ON_CLOSE order spec.
    from decimal import Decimal as D

    from msts_trader.models import Order, Side

    captured = {}
    b = _broker()
    b._client = SimpleNamespace(
        place_order=lambda h, spec: (
            captured.update(spec=spec) or _OrderResp("https://api.schwab.com/v1/accounts/HASH/orders/777")
        )
    )
    r = b.place_market(Order(ticker="SPY", side=Side.BUY, quantity=D("10"), moc=True))
    assert r["status"] == "submitted" and r["moc"] is True
    assert captured["spec"].get("orderType") == "MARKET_ON_CLOSE"


def test_place_market_without_moc_stays_market():
    from decimal import Decimal as D

    from msts_trader.models import Order, Side

    captured = {}
    b = _broker()
    b._client = SimpleNamespace(
        place_order=lambda h, spec: (
            captured.update(spec=spec) or _OrderResp("https://api.schwab.com/v1/accounts/HASH/orders/778")
        )
    )
    b.place_market(Order(ticker="SPY", side=Side.BUY, quantity=D("10")))
    assert captured["spec"].get("orderType") == "MARKET"


def test_place_market_error():
    from msts_trader.models import Order, Side
    from decimal import Decimal as D

    b = _broker()
    b._client = SimpleNamespace(place_order=lambda h, spec: (_ for _ in ()).throw(RuntimeError("401 unauthorized")))
    r = b.place_market(Order(ticker="SPY", side=Side.BUY, quantity=D("10")))
    assert r["status"] == "error" and "401" in r["reason"]


# ----- protective stops (regression: these methods were once mis-indented
# into the module-level clear_token() and silently absent from the class) -----


def test_place_stop_builds_gtc_stop_spec():
    captured = {}
    b = _broker()
    b._client = SimpleNamespace(
        place_order=lambda h, spec: (
            captured.update(spec=spec) or _OrderResp("https://api.schwab.com/v1/accounts/HASH/orders/55501")
        )
    )
    r = b.place_stop("SPY", Decimal("10.9"), Decimal("480"))
    assert r["status"] == "submitted" and r["order_id"] == "55501"
    assert r["quantity"] == 10  # whole shares (int truncation)
    spec = captured["spec"]
    assert spec.get("orderType") == "STOP"
    assert spec.get("stopPrice") == "480.00"
    assert spec.get("duration") == "GOOD_TILL_CANCEL"


def test_place_stop_dry_run_sends_nothing():
    called = {"placed": False}
    b = _broker()
    b._client = SimpleNamespace(place_order=lambda h, spec: called.update(placed=True))
    r = b.place_stop("SPY", Decimal("10"), Decimal("480"), dry_run=True)
    assert r["status"] == "dry-run" and r["dry_run"] is True
    assert called["placed"] is False


def test_place_stop_sub_share_skips():
    r = _broker().place_stop("SPY", Decimal("0.4"), Decimal("480"))
    assert r["status"] == "skipped" and "whole-share" in r["reason"]


def test_open_stops_filters_and_keys_by_symbol():
    orders = [
        {
            "orderType": "STOP",
            "orderId": 9001,
            "stopPrice": 475.5,
            "orderLegCollection": [{"instrument": {"symbol": "SPY"}, "quantity": 10}],
        },
        {
            "orderType": "MARKET",
            "orderId": 9002,  # not a stop -> ignored
            "orderLegCollection": [{"instrument": {"symbol": "QQQ"}, "quantity": 5}],
        },
    ]
    b = _broker()
    b._client = SimpleNamespace(get_orders_for_account=lambda h, status=None: _Resp(orders))
    out = b.open_stops()
    assert set(out) == {"SPY"}
    assert out["SPY"][0]["order_id"] == "9001"
    assert out["SPY"][0]["stop_price"] == Decimal("475.5")


def test_open_stops_swallows_errors():
    b = _broker()
    b._client = SimpleNamespace(
        get_orders_for_account=lambda h, status=None: (_ for _ in ()).throw(RuntimeError("403"))
    )
    assert b.open_stops() == {}


def test_cancel_order_ok_and_error():
    b = _broker()
    b._client = SimpleNamespace(cancel_order=lambda oid, h: _OrderResp())
    assert b.cancel_order("9001")["status"] == "CANCELLED"
    b._client = SimpleNamespace(cancel_order=lambda oid, h: (_ for _ in ()).throw(RuntimeError("404")))
    r = b.cancel_order("9001")
    assert r["status"] == "error" and "404" in r["reason"]


def test_balances_zero_values_do_not_fall_through():
    # A legitimate 0 (account in liquidation, exhausted BP) must not fall
    # through to the secondary field.
    acct = {
        "securitiesAccount": {
            "currentBalances": {
                "liquidationValue": 0,
                "equity": 50000,
                "cashBalance": 0,
                "buyingPower": 0,
                "dayTradingBuyingPower": 99999,
            }
        }
    }
    bal = _broker(account=acct).balances()
    assert bal.nav == Decimal("0")
    assert bal.cash == Decimal("0")
    assert bal.buying_power == Decimal("0")


def test_schwab_init_falls_back_to_manual_flow_when_no_browser(tmp_path, monkeypatch):
    import webbrowser

    monkeypatch.setattr(schwab_mod.keychain, "load_secret", lambda name: None)
    monkeypatch.setattr(schwab_mod.keychain, "save_secret", lambda name, value: None)
    monkeypatch.setattr(schwab_mod, "TOKEN_PATH", tmp_path / "schwab_token.json")

    called = {"login": 0, "manual": 0}

    class _Client:
        def get_account_numbers(self):
            return _Resp([{"hashValue": "HASH_MANUAL"}])

    def fake_login_flow(**kwargs):
        called["login"] += 1
        raise webbrowser.Error("could not locate runnable browser")

    def fake_manual_flow(**kwargs):
        called["manual"] += 1
        return _Client()

    monkeypatch.setattr(schwab_mod, "client_from_login_flow", fake_login_flow)
    monkeypatch.setattr(schwab_mod, "client_from_manual_flow", fake_manual_flow)

    b = Schwab("app-key", "app-secret")
    assert b.account_hash == "HASH_MANUAL"
    assert called == {"login": 1, "manual": 1}

    # Explicit manual=True skips client_from_login_flow altogether
    b2 = Schwab("app-key", "app-secret", manual=True)
    assert b2.account_hash == "HASH_MANUAL"
    assert called == {"login": 1, "manual": 2}


def test_schwab_web_auth_helpers_and_token_status(tmp_path, monkeypatch):
    import time

    store: dict[str, str] = {}
    monkeypatch.setattr(schwab_mod.keychain, "load_secret", lambda name: store.get(name))
    monkeypatch.setattr(schwab_mod.keychain, "save_secret", lambda name, value: store.__setitem__(name, value))
    monkeypatch.setattr(schwab_mod.keychain, "load", lambda broker: {"app_key": "ak", "app_secret": "as"})
    monkeypatch.setattr(schwab_mod, "TOKEN_PATH", tmp_path / "schwab_token.json")

    st0 = schwab_mod.token_status()
    assert st0["has_creds"] is True and st0["has_token"] is False

    ctx = schwab_mod.start_web_auth("ak", "https://127.0.0.1:8182")
    assert "api.schwabapi.com" in ctx["authorization_url"] and ctx["state"]

    class _Client:
        def token_age(self):
            return 10

        def get_account_numbers(self):
            return _Resp([{"hashValue": "HASH_WEB", "accountNumber": "12345678"}])

    def fake_from_received_url(api_key, app_secret, auth_context, received_url, token_write_func, enforce_enums=True):
        assert api_key == "ak" and app_secret == "as"
        assert auth_context.state == ctx["state"]
        assert "code=ABC" in received_url
        token_write_func({"creation_timestamp": int(time.time()), "token": {"access_token": "A", "refresh_token": "R"}})
        return _Client()

    monkeypatch.setattr(schwab_mod, "client_from_received_url", fake_from_received_url)
    monkeypatch.setattr(schwab_mod, "client_from_access_functions", lambda *a, **k: _Client())

    b = schwab_mod.complete_web_auth(
        app_key="ak",
        app_secret="as",
        callback_url=ctx["callback_url"],
        state=ctx["state"],
        received_url="https://127.0.0.1:8182/?code=ABC&session=XYZ",
    )
    assert b.account_hash == "HASH_WEB"
    st1 = schwab_mod.token_status()
    assert st1["has_token"] is True and st1["expired"] is False and (st1["expires_in_seconds"] or 0) > 500000
