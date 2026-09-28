"""Symphony engine: indicators, evaluation, backtest, Composer import."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("pandas")

from msts_trader.csv_parser import parse_csv  # noqa: E402
from msts_trader.symphony import backtest, indicators  # noqa: E402
from msts_trader.symphony.composer_import import ComposerImportError, import_text, parse_edn  # noqa: E402
from msts_trader.symphony.evaluate import Engine, EvalError, evaluate, to_csv  # noqa: E402
from msts_trader.symphony.model import INDICATORS, Symphony, tickers  # noqa: E402


def sym(children, **kw) -> Symphony:
    return Symphony.model_validate({"id": "t", "name": "T", "children": children, **kw})


def asset(t, **kw):
    return {"step": "asset", "ticker": t, **kw}


@pytest.fixture
def closes() -> pd.DataFrame:
    idx = pd.bdate_range("2021-01-01", periods=400)
    rng = np.random.default_rng(7)
    data = {}
    for t, vol in (("SPY", 0.01), ("QQQ", 0.015), ("TLT", 0.008), ("BIL", 0.0005)):
        data[t] = 100 * np.cumprod(1 + rng.normal(0.0003, vol, len(idx)))
    return pd.DataFrame(data, index=idx)


# ── indicators ────────────────────────────────────────────────────────────
WILDER = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28,
          46.00, 46.03, 46.41, 46.22, 45.64]  # fmt: skip


def test_rsi_matches_wilder_reference():
    # StockCharts' published 14-day Wilder RSI example.
    r = indicators.compute("relative-strength-index", pd.Series(WILDER), 14).to_numpy()
    assert np.isnan(r[13])
    assert r[14] == pytest.approx(70.46, abs=0.1)
    assert r[15] == pytest.approx(66.25, abs=0.1)
    assert r[19] == pytest.approx(57.92, abs=0.2)


def test_simple_indicators():
    c = pd.Series([100.0, 110.0, 99.0, 108.9])
    assert indicators.compute("cumulative-return", c, 2).iloc[-1] == pytest.approx(-1.0)
    assert indicators.compute("moving-average-price", c, 2).iloc[-1] == pytest.approx(103.95)
    assert indicators.compute("moving-average-return", c, 3).iloc[-1] == pytest.approx((10 - 10 + 10) / 3)
    assert indicators.compute("max-drawdown", c, 4).iloc[-1] == pytest.approx(10.0)
    assert indicators.compute("current-price", c, 1).iloc[-1] == 108.9


@pytest.mark.parametrize("fn", INDICATORS)
def test_indicators_are_causal(fn):
    """Changing future prices must not change any past value."""
    rng = np.random.default_rng(1)
    c = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.01, 200)))
    base = indicators.compute(fn, c, 10).to_numpy()
    c2 = c.copy()
    c2.iloc[150:] *= 1.5
    moved = indicators.compute(fn, c2, 10).to_numpy()
    np.testing.assert_array_equal(base[:150], moved[:150])


# ── evaluation ────────────────────────────────────────────────────────────
def test_equal_and_specified(closes):
    w, _ = evaluate(sym([{"step": "wt-cash-equal", "children": [asset("SPY"), asset("TLT")]}]), closes)
    assert w == pytest.approx({"SPY": 0.5, "TLT": 0.5})
    w, _ = evaluate(
        sym([{"step": "wt-cash-specified", "children": [asset("SPY", weight=0.6), asset("TLT", weight=0.4)]}]),
        closes,
    )
    assert w == pytest.approx({"SPY": 0.6, "TLT": 0.4})


def test_inverse_vol_favours_low_vol(closes):
    w, _ = evaluate(sym([{"step": "wt-inverse-vol", "window": 20, "children": [asset("QQQ"), asset("TLT")]}]), closes)
    t = len(closes) - 1
    eng = Engine(sym([]), closes)
    vq = eng.indicator("standard-deviation-return", "QQQ", 20, t)
    vt = eng.indicator("standard-deviation-return", "TLT", 20, t)
    assert w["TLT"] / w["QQQ"] == pytest.approx(vq / vt)
    assert sum(w.values()) == pytest.approx(1.0)


def test_if_branches(closes):
    def s(value):
        return sym(
            [
                {
                    "step": "if",
                    "condition": {
                        "lhs": {"fn": "current-price", "ticker": "SPY"},
                        "comparator": "gt",
                        "rhs_value": value,
                    },
                    "then": [asset("SPY")],
                    "else": [asset("BIL")],
                }
            ]
        )

    assert evaluate(s(0), closes)[0] == {"SPY": 1.0}
    assert evaluate(s(1e9), closes)[0] == {"BIL": 1.0}


def test_if_metric_vs_metric_and_empty_branch_is_cash(closes):
    s = sym(
        [
            {
                "step": "if",
                "condition": {
                    "lhs": {"fn": "current-price", "ticker": "SPY"},
                    "comparator": "gt",
                    "rhs": {"fn": "current-price", "ticker": "SPY"},
                },
                "then": [asset("SPY")],
                "else": [],
            }
        ]
    )
    assert evaluate(s, closes)[0] == {}


def test_filter_top_and_bottom(closes):
    t = len(closes) - 1
    rets = {k: closes[k].iloc[t] / closes[k].iloc[t - 10] - 1 for k in ("SPY", "QQQ", "TLT")}
    order = sorted(rets, key=rets.get, reverse=True)
    kids = [asset(k) for k in ("SPY", "QQQ", "TLT")]
    top = evaluate(sym([{"step": "filter", "sort_fn": "cumulative-return", "window": 10, "n": 2, "children": kids}]), closes)[0]  # fmt: skip
    assert set(top) == set(order[:2])
    bot = evaluate(sym([{"step": "filter", "sort_fn": "cumulative-return", "window": 10, "select": "bottom", "n": 1, "children": kids}]), closes)[0]  # fmt: skip
    assert set(bot) == {order[-1]}


def test_filter_can_rank_groups(closes):
    s = sym(
        [
            {
                "step": "filter",
                "sort_fn": "cumulative-return",
                "window": 10,
                "n": 1,
                "children": [
                    {"step": "group", "name": "A", "children": [asset("SPY")]},
                    {"step": "group", "name": "B", "children": [asset("TLT")]},
                ],
            }
        ]
    )
    w, _ = evaluate(s, closes)
    t = len(closes) - 1
    spy = closes["SPY"].iloc[t] / closes["SPY"].iloc[t - 10]
    tlt = closes["TLT"].iloc[t] / closes["TLT"].iloc[t - 10]
    assert w == {("SPY" if spy > tlt else "TLT"): 1.0}


def test_insufficient_history_is_a_clear_error(closes):
    s = sym([{"step": "filter", "sort_fn": "cumulative-return", "window": 300, "children": [asset("SPY")]}])
    with pytest.raises(EvalError, match="insufficient history"):
        Engine(s, closes).weights(100)


def test_csv_round_trips_through_parser(closes):
    w, _ = evaluate(sym([{"step": "wt-inverse-vol", "window": 20, "children": [asset("SPY"), asset("TLT")]}]), closes)
    targets = parse_csv(to_csv(w))
    assert {t.ticker for t in targets} == {"SPY", "TLT"}
    assert float(sum(t.weight for t in targets)) == pytest.approx(1.0, abs=1e-5)
    # all-cash still yields a parseable CSV that zeroes the fallback ticker
    zero = parse_csv(to_csv({}, fallback="SPY"))
    assert zero[0].ticker == "SPY" and zero[0].weight == 0


def test_tickers_collects_condition_tickers():
    s = sym(
        [
            {
                "step": "if",
                "condition": {"lhs": {"fn": "current-price", "ticker": "VIXY"}, "comparator": "gt", "rhs_value": 1},
                "then": [asset("SPY")],
                "else": [asset("bil")],
            }
        ]
    )
    assert tickers(s) == ["BIL", "SPY", "VIXY"]


def test_condition_needs_exactly_one_rhs():
    with pytest.raises(Exception, match="exactly one"):
        sym(
            [
                {
                    "step": "if",
                    "condition": {"lhs": {"fn": "current-price", "ticker": "SPY"}, "comparator": "gt"},
                    "then": [],
                }
            ]
        )


# ── backtest ──────────────────────────────────────────────────────────────
def test_single_asset_equals_buy_and_hold(closes):
    r = backtest.run(sym([asset("SPY")]), closes, cost_bps=0)
    t0 = closes.index.get_loc(pd.Timestamp(r["start"]))
    expected = closes["SPY"].iloc[-1] / closes["SPY"].iloc[t0]
    assert r["equity"][-1] == pytest.approx(expected, rel=1e-5)
    assert r["benchmark"]["equity"][-1] == pytest.approx(expected, rel=1e-5)


def test_lookahead_canary():
    """X alternates +2% / -2%. 'Hold X if it rose today' only wins by peeking at
    tomorrow; without lookahead it always holds X into a down day and loses."""
    n = 200
    px = [100.0]
    for i in range(1, n):
        px.append(px[-1] * (1.02 if i % 2 else 0.98))
    idx = pd.bdate_range("2022-01-03", periods=n)
    df = pd.DataFrame({"X": px, "CASH": [100.0] * n}, index=idx)
    s = sym(
        [
            {
                "step": "if",
                "condition": {
                    "lhs": {"fn": "cumulative-return", "ticker": "X", "window": 1},
                    "comparator": "gt",
                    "rhs_value": 0,
                },
                "then": [asset("X")],
                "else": [asset("CASH")],
            }
        ]
    )
    r = backtest.run(s, df, cost_bps=0, benchmark=None)
    assert r["equity"][-1] < 0.5


def test_costs_reduce_returns(closes):
    s = sym(
        [
            {
                "step": "filter",
                "sort_fn": "cumulative-return",
                "window": 5,
                "n": 1,
                "children": [asset("SPY"), asset("QQQ"), asset("TLT")],
            }
        ]
    )
    free = backtest.run(s, closes, cost_bps=0)
    costly = backtest.run(s, closes, cost_bps=25)
    assert costly["equity"][-1] < free["equity"][-1]
    assert free["metrics"]["annual_turnover"] > 1


def test_monthly_rebalance_trades_less(closes):
    kids = [{"step": "wt-cash-equal", "children": [asset("SPY"), asset("TLT")]}]
    daily = backtest.run(sym(kids), closes)
    monthly = backtest.run(sym(kids, rebalance="monthly"), closes)
    assert len(monthly["allocations"]) <= 20 and len(daily["allocations"]) < len(daily["dates"]) / 5
    assert monthly["metrics"]["annual_turnover"] < daily["metrics"]["annual_turnover"]


# ── Composer import ───────────────────────────────────────────────────────
COMPOSER_EDN = """
{:id "abc123", :step :root, :name "RSI Rotator", :rebalance :daily,
 :children
 [{:step :wt-cash-equal,
   :children
   [{:step :if,
     :children
     [{:step :if-child, :is-else-condition? false, :comparator :gt,
       :lhs-fn :relative-strength-index, :lhs-window-days "10", :lhs-val "SPY",
       :rhs-fixed-value? true, :rhs-val "79",
       :children [{:step :asset, :ticker "UVXY", :name "ProShares Ultra VIX"}]}
      {:step :if-child, :is-else-condition? false, :comparator :lt,
       :lhs-fn :current-price, :lhs-val "SPY",
       :rhs-fn :moving-average-price, :rhs-fn-params {:window 200}, :rhs-val "SPY",
       :children [{:step :group, :name "Safety",
                   :children [{:step :wt-cash-specified,
                               :children [{:step :asset :ticker "BIL" :weight {:num 60 :den 100}}
                                          {:step :asset :ticker "TLT" :weight {:num 40 :den 100}}]}]}]}
      {:step :if-child, :is-else-condition? true,
       :children [{:step :filter, :sort-by-fn :cumulative-return, :sort-by-window-days "20",
                   :select-fn :top, :select-n "2",
                   :children [{:step :asset :ticker "TQQQ"} {:step :asset :ticker "SOXL"}
                              {:step :wt-inverse-vol :window-days "10"
                               :children [{:step :asset :ticker "SPY"} {:step :empty}]}]}]}]}]}]}
"""


def test_edn_reader_basics():
    assert parse_edn('{:a [1 2.5 "x" nil true :kw]}') == {"a": [1, 2.5, "x", None, True, "kw"]}


def test_composer_edn_import():
    s, warnings = import_text(COMPOSER_EDN)
    assert s.id == "rsi-rotator" and s.name == "RSI Rotator" and warnings == []
    top_if = s.children[0].children[0]
    assert top_if.step == "if"
    assert top_if.condition.lhs.fn == "relative-strength-index" and top_if.condition.lhs.window == 10
    assert top_if.condition.rhs_value == 79
    elif_ = top_if.otherwise[0]  # else-if chain became a nested if
    assert elif_.condition.rhs.fn == "moving-average-price" and elif_.condition.rhs.window == 200
    spec = elif_.then[0].children[0]
    assert [c.weight for c in spec.children] == pytest.approx([0.6, 0.4])
    filt = elif_.otherwise[0]
    assert filt.sort_fn == "cumulative-return" and filt.window == 20 and filt.n == 2
    assert filt.children[2].window == 10 and len(filt.children[2].children) == 1  # :empty dropped as cash
    assert tickers(s) == ["BIL", "SOXL", "SPY", "TLT", "TQQQ", "UVXY"]


def test_composer_json_with_keyword_strings():
    text = (
        '{"step": ":root", "name": "J", "rebalance": ":monthly", "children": '
        '[{"step": ":wt-cash-equal", "children": [{"step": ":asset", "ticker": "SPY"}]}]}'
    )
    s, _ = import_text(text)
    assert s.rebalance == "monthly" and s.children[0].children[0].ticker == "SPY"


def test_unsupported_nodes_are_reported_not_dropped():
    bad = '{:step :root :name "X" :children [{:step :wt-cash-equal :children [{:step :mystery} {:step :filter :sort-by-fn :alpha-magic :children []}]}]}'  # noqa: E501
    with pytest.raises(ComposerImportError) as e:
        import_text(bad)
    msg = str(e.value)
    assert "root/0/0: unsupported block 'mystery'" in msg
    assert "unsupported indicator 'alpha-magic'" in msg


def test_corridor_rebalance_warns():
    s, warnings = import_text('{:step :root :name "C" :rebalance :none :children [{:step :asset :ticker "SPY"}]}')
    assert s.rebalance == "daily" and warnings


def test_native_format_round_trips(closes):
    s = sym([{"step": "wt-cash-equal", "children": [asset("SPY"), asset("TLT", weight=0.3)]}])
    again, _ = import_text(s.model_dump_json(by_alias=True))
    assert again == s
