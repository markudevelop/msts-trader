"""Hyperliquid adapter parsing — mock the Info client (no network)."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from msts_trader.brokers.hyperliquid import Hyperliquid, _coin


def _broker(user_state=None, mids=None):
    b = Hyperliquid.__new__(Hyperliquid)
    b._address = "0xabc0000000000000000000000000000000000def"
    b.account_id = "0xabc…0def"
    b._meta = None
    b._info = SimpleNamespace(
        user_state=lambda addr: user_state or {},
        all_mids=lambda: mids or {},
    )
    return b


def test_balances():
    st = {"marginSummary": {"accountValue": "12345.67"}, "withdrawable": "1000.00"}
    bal = _broker(user_state=st).balances()
    assert bal.nav == Decimal("12345.67")
    assert bal.cash == Decimal("1000.00")
    assert bal.buying_power == Decimal("1000.00")


def test_positions_filters_flat_and_reads_szi():
    st = {"assetPositions": [
        {"position": {"coin": "BTC", "szi": "0.5", "entryPx": "60000"}},
        {"position": {"coin": "ETH", "szi": "0"}},     # flat -> skipped
        {"position": {"coin": "SOL", "szi": "-10", "entryPx": "150"}},  # short kept
    ]}
    out = _broker(user_state=st).positions()
    assert set(out) == {"BTC", "SOL"}
    assert out["BTC"].quantity == Decimal("0.5")
    assert out["BTC"].price == Decimal("60000")
    assert out["SOL"].quantity == Decimal("-10")


def test_quote_normalises_tickers():
    mids = {"BTC": "61000.5", "ETH": "3000"}
    out = _broker(mids=mids).quote(["BTC-USD", "eth", "DOGE"])
    assert out["BTC"] == Decimal("61000.5")
    assert out["ETH"] == Decimal("3000")
    assert "DOGE" not in out  # no mid for it


def test_quote_swallows_errors():
    b = Hyperliquid.__new__(Hyperliquid)
    b._info = SimpleNamespace(all_mids=lambda: (_ for _ in ()).throw(RuntimeError("down")))
    assert b.quote(["BTC"]) == {}


def test_coin_normalisation():
    assert _coin("BTC-USD") == "BTC"
    assert _coin("eth/usdc") == "ETH"
    assert _coin("SOL") == "SOL"
