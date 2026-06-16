"""Unit tests for pure helper functions inside the broker adapters.

These don't touch any live SDK — they exercise the parsing/normalisation
logic that real bugs (like the IBKR KID/10349 surfacing) live in.
"""
from __future__ import annotations

from types import SimpleNamespace

from msts_trader.brokers import hyperliquid as hl
from msts_trader.brokers import ibkr


def _trade(*log_entries):
    """Build a fake ib_insync trade with a .log of (errorCode, message)."""
    log = [SimpleNamespace(errorCode=code, message=msg) for code, msg in log_entries]
    return SimpleNamespace(log=log)


# ----- ibkr._reject_reason (safety-critical: surfaces why an order died) -----

def test_reject_reason_prefers_specific_over_10349():
    t = _trade((10349, "TIF set to DAY based on order preset."),
               (201, "No Trading Permission ... does not have a KID ..."))
    msg = ibkr._reject_reason(t)
    assert msg.startswith("IBKR 201")
    assert "KID" in msg


def test_reject_reason_10349_fallback_with_hint():
    t = _trade((10349, "TIF set to DAY based on order preset."))
    msg = ibkr._reject_reason(t)
    assert "IBKR 10349" in msg
    assert "Presets" in msg  # points at the TWS fix


def test_reject_reason_10243_suggests_whole_shares():
    t = _trade((10243, "Fractional-sized order cannot be placed via API. Please use desktop version to place this order."))
    msg = ibkr._reject_reason(t)
    assert "IBKR 10243" in msg
    assert "--whole-shares" in msg  # points at the fix


def test_reject_reason_10243_beats_10349_noise():
    # When both fire, the actionable fractional message wins over the cosmetic
    # 10349 preset note.
    t = _trade((10349, "TIF set to DAY based on order preset."),
               (10243, "Fractional-sized order cannot be placed via API."))
    msg = ibkr._reject_reason(t)
    assert "10243" in msg and "--whole-shares" in msg


def test_reject_reason_none_when_clean():
    assert ibkr._reject_reason(_trade((0, ""), (0, "Submitted"))) is None


def test_reject_reason_strips_br():
    t = _trade((201, "line one<br>line two<br>"))
    msg = ibkr._reject_reason(t)
    assert "<br>" not in msg
    assert "line one line two" in msg


def test_reject_reason_empty_log():
    assert ibkr._reject_reason(SimpleNamespace(log=[])) is None


# ----- ibkr._f (NaN-safe float) -----

def test_f_none():
    assert ibkr._f(None) is None


def test_f_nan():
    assert ibkr._f(float("nan")) is None


def test_f_valid():
    assert ibkr._f("123.45") == 123.45


# ----- ibkr._midpoint -----

def test_midpoint_normal():
    assert ibkr._midpoint(100, 102) == 101.0


def test_midpoint_missing():
    assert ibkr._midpoint(None, 102) is None
    assert ibkr._midpoint(100, None) is None


def test_midpoint_nonpositive():
    assert ibkr._midpoint(0, 102) is None
    assert ibkr._midpoint(-1, 102) is None


# ----- hyperliquid._coin (ticker normalisation) -----

def test_coin_strips_usd_suffix():
    assert hl._coin("BTC-USD") == "BTC"
    assert hl._coin("ETH-USDC") == "ETH"
    assert hl._coin("SOL/USD") == "SOL"
    assert hl._coin("BTC-PERP") == "BTC"


def test_coin_passthrough_and_upper():
    assert hl._coin("btc") == "BTC"
    assert hl._coin(" eth ") == "ETH"
    assert hl._coin("DOGE") == "DOGE"
