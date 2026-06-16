"""Structural tests — every adapter must expose the Broker protocol.

These don't connect to any broker; they check the class shape so a
typo'd method or missing attribute fails fast in CI.
"""
from __future__ import annotations

import inspect

import pytest

from msts_trader.brokers import SUPPORTED, BrokerError, make


REQUIRED_ATTRS = ("name", "supports_fractional", "supports_moc")
REQUIRED_METHODS = ("balances", "positions", "quote", "place_market")


def _broker_classes():
    from msts_trader.brokers.alpaca import Alpaca
    from msts_trader.brokers.hyperliquid import Hyperliquid
    from msts_trader.brokers.ibkr import IBKR
    from msts_trader.brokers.paper import Paper
    from msts_trader.brokers.schwab import Schwab
    from msts_trader.brokers.tastytrade import Tastytrade
    from msts_trader.brokers.tradier import Tradier
    return {
        "tastytrade": Tastytrade,
        "alpaca": Alpaca,
        "tradier": Tradier,
        "ibkr": IBKR,
        "schwab": Schwab,
        "hyperliquid": Hyperliquid,
        "paper": Paper,
    }


@pytest.mark.parametrize("name", SUPPORTED)
def test_broker_class_has_required_class_attrs(name):
    cls = _broker_classes()[name]
    assert getattr(cls, "name", None) == name
    assert hasattr(cls, "supports_fractional")
    assert isinstance(cls.supports_fractional, bool)
    assert hasattr(cls, "supports_moc")
    assert isinstance(cls.supports_moc, bool)


def test_moc_support_matrix():
    # The CLI's --moc error message and README promise exactly this set.
    classes = _broker_classes()
    supported = {n for n, cls in classes.items() if cls.supports_moc}
    assert supported == {"alpaca", "ibkr", "schwab", "paper"}


@pytest.mark.parametrize("name", SUPPORTED)
def test_supports_stops_is_bool(name):
    cls = _broker_classes()[name]
    assert hasattr(cls, "supports_stops")
    assert isinstance(cls.supports_stops, bool)


STOP_METHODS = ("place_stop", "open_stops", "cancel_order")


@pytest.mark.parametrize("name", SUPPORTED)
def test_supports_stops_implies_methods_bound(name):
    """Regression guard for the 0.13/0.14 bug: IBKR's *and* Schwab's stop
    methods were mis-indented into a module-level function, so the class
    silently lost them and every stop path raised AttributeError at runtime
    (`'IBKR' object has no attribute 'open_stops'`). A broker that advertises
    supports_stops MUST define all three on the class itself — checked via the
    class __dict__ so a method leaked to module scope can't satisfy it."""
    cls = _broker_classes()[name]
    if not cls.supports_stops:
        return
    for m in STOP_METHODS:
        assert m in vars(cls), f"{name} declares supports_stops but {m} is not defined on the class"
        assert callable(getattr(cls, m)), f"{name}.{m} not callable"


def test_stops_support_matrix():
    # README/CHANGELOG promise 6 of 7 brokers; only hyperliquid abstains
    # (perps use trigger-order semantics, never on the equity weights path).
    classes = _broker_classes()
    supported = {n for n, cls in classes.items() if cls.supports_stops}
    assert supported == {"paper", "tastytrade", "alpaca", "tradier", "ibkr", "schwab"}


@pytest.mark.parametrize("name", SUPPORTED)
@pytest.mark.parametrize("method", REQUIRED_METHODS)
def test_broker_class_has_required_methods(name, method):
    cls = _broker_classes()[name]
    fn = getattr(cls, method, None)
    assert callable(fn), f"{name}.{method} missing or not callable"


@pytest.mark.parametrize("name", SUPPORTED)
def test_place_market_signature_accepts_dry_run(name):
    cls = _broker_classes()[name]
    sig = inspect.signature(cls.place_market)
    assert "dry_run" in sig.parameters, f"{name}.place_market must accept dry_run"


def test_make_rejects_unknown_broker():
    with pytest.raises(BrokerError, match="unknown broker"):
        make("nonexistent")


def test_make_lists_supported_in_error():
    with pytest.raises(BrokerError, match="supported:"):
        make("nope")
