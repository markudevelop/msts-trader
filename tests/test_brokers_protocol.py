"""Structural tests — every adapter must expose the Broker protocol.

These don't connect to any broker; they check the class shape so a
typo'd method or missing attribute fails fast in CI.
"""
from __future__ import annotations

import inspect

import pytest

from msts_trader.brokers import SUPPORTED, BrokerError, make


REQUIRED_ATTRS = ("name", "supports_fractional")
REQUIRED_METHODS = ("balances", "positions", "quote", "place_market")


def _broker_classes():
    from msts_trader.brokers.alpaca import Alpaca
    from msts_trader.brokers.ibkr import IBKR
    from msts_trader.brokers.paper import Paper
    from msts_trader.brokers.schwab import Schwab
    from msts_trader.brokers.tastytrade import Tastytrade
    return {
        "tastytrade": Tastytrade,
        "alpaca": Alpaca,
        "ibkr": IBKR,
        "schwab": Schwab,
        "paper": Paper,
    }


@pytest.mark.parametrize("name", SUPPORTED)
def test_broker_class_has_required_class_attrs(name):
    cls = _broker_classes()[name]
    assert getattr(cls, "name", None) == name
    assert hasattr(cls, "supports_fractional")
    assert isinstance(cls.supports_fractional, bool)


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
