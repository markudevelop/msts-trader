"""Regressions from the first field reports on Studio (0.39.0):

1. A config-file `allocation` made every funded strategy fail with "sleeve has
   its own sizing — drop --allocation" (Studio never passes --allocation).
2. Composer exports with structured / compound conditions failed to import.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from msts_trader.symphony import backtest, runner  # noqa: E402
from msts_trader.symphony.composer_import import ComposerImportError, import_text  # noqa: E402
from msts_trader.symphony.evaluate import evaluate  # noqa: E402
from msts_trader.symphony.model import Condition, Symphony, tickers  # noqa: E402
from tests.test_studio import _closes, make_sym, studio_home  # noqa: E402,F401  (autouse: isolates ~/.msts-trader)


# ── 1. config-file allocation vs self-funded sleeves ──────────────────────
def _invest(sid: str, amount: str) -> None:
    r = subprocess.run(
        [sys.executable, "-m", "msts_trader", "sleeve", "invest", sid, amount, "--broker", "paper"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_config_allocation_no_longer_blocks_funded_strategy():
    home = Path(os.environ["HOME"])  # the isolated home set by studio_home
    (home / ".msts-trader" / "config.toml").write_text("allocation = 50000\n", encoding="utf-8")
    _invest("momo", "10000")
    s = make_sym(live_enabled=True)
    dry = runner.run(s, mode="dry")
    assert dry["status"] == "preview", dry
    notional = sum(float(o["notional"]) for o in dry["preview"]["orders"])
    assert notional == pytest.approx(10000, rel=0.05)  # the sleeve's $10k, not the config's $50k
    live = runner.run(s, mode="live")
    assert live["status"] == "executed", live


def test_explicit_allocation_flag_still_refused_for_funded_sleeve(tmp_path):
    _invest("momo", "10000")
    csv = tmp_path / "w.csv"
    csv.write_text("ticker,weight\nSPY,1\n", encoding="utf-8")
    r = subprocess.run(
        [sys.executable, "-m", "msts_trader", "rebalance", "--broker", "paper", "--csv-file", str(csv),
         "--sleeve", "momo", "--allocation", "5000", "--dry-run", "--json"],
        capture_output=True,
        text=True,
    )  # fmt: skip
    assert r.returncode != 0 and "has its own sizing" in r.stdout


# ── 2. compound conditions ────────────────────────────────────────────────
RSI_SPY = {"fn": "relative-strength-index", "ticker": "SPY", "window": 10}


def _cmp(value, comparator="gt", metric=RSI_SPY):
    return {"lhs": metric, "comparator": comparator, "rhs_value": value}


def test_condition_model_shapes():
    assert Condition.model_validate({"any": [_cmp(1), _cmp(2)]}).metrics()[0].ticker == "SPY"
    for bad in ({"any": []}, {"any": [_cmp(1)], "all": [_cmp(1)]}, {"any": [_cmp(1)], **_cmp(1)}, {"lhs": RSI_SPY}):
        with pytest.raises(Exception):
            Condition.model_validate(bad)


def _if(cond):
    return Symphony.model_validate(
        {
            "id": "c",
            "name": "C",
            "children": [{"step": "if", "condition": cond, "then": [{"step": "asset", "ticker": "SPY"}],
                          "else": [{"step": "asset", "ticker": "TLT"}]}],
        }
    )  # fmt: skip


def test_any_all_evaluate():
    closes = _closes(["SPY", "TLT", "QQQ"])
    true, false = _cmp(-1), _cmp(1000)  # RSI is always within 0..100
    assert evaluate(_if({"any": [false, true]}), closes)[0] == {"SPY": 1.0}
    assert evaluate(_if({"all": [true, false]}), closes)[0] == {"TLT": 1.0}
    assert evaluate(_if({"all": [true, {"any": [false, true]}]}), closes)[0] == {"SPY": 1.0}
    # nested metrics count for tickers and warm-up
    s = _if({"any": [false, _cmp(50, metric={"fn": "moving-average-price", "ticker": "QQQ", "window": 200})]})
    assert "QQQ" in tickers(s)
    backtest.run(s, closes, cost_bps=0)  # warm-up covers the nested 200-day window (no EvalError)


def test_condition_round_trips_json():
    s = _if({"all": [_cmp(30), {"any": [_cmp(70, "lt"), _cmp(10)]}]})
    again = Symphony.model_validate_json(s.model_dump_json(by_alias=True))
    assert again == s


# ── Composer import: structured conditions (the newer editor) ────────────
def _composer(condition: dict, legacy: dict | None = None) -> str:
    child = {"step": "if-child", "is-else-condition?": False, "condition": condition, **(legacy or {}),
             "children": [{"step": "asset", "ticker": "SPY"}]}  # fmt: skip
    root = {
        "step": "root",
        "name": "Structured",
        "rebalance": "daily",
        "children": [{"step": "if", "children": [child, {"step": "if-child", "is-else-condition?": True,
                                                         "children": [{"step": "asset", "ticker": "SHV"}]}]}],
    }  # fmt: skip
    return json.dumps(root)


def test_import_binary_compound_with_ticker_placeholder():
    cond = {
        "condition-type": "binary-compound",
        "lhs": {"fn": "relative-strength-index", "ticker": "%", "params": {"window": 10}},
        "tickers": ["QQQ", "SPY"],
        "comparator": "lt",
        "operator": "all",
        "rhs": {"constant": 35},
    }
    s, _ = import_text(_composer(cond))
    c = s.children[0].condition
    assert [x.lhs.ticker for x in c.all] == ["QQQ", "SPY"] and c.all[0].rhs_value == 35 and c.all[0].comparator == "lt"


def test_import_compound_with_metric_rhs_and_single_ticker_collapse():
    cond = {
        "condition-type": "compound",
        "operator": "any",
        "conditions": [
            {"condition-type": "binary", "comparator": "gt",
             "lhs": {"fn": "relative-strength-index", "ticker": "SPY", "params": {"window": 10}},
             "rhs": {"fn": "relative-strength-index", "ticker": "KMLM", "params": {"window": 10}}},
            {"condition-type": "binary-compound", "operator": "any", "tickers": ["VIXY"], "comparator": "gt",
             "lhs": {"fn": "max-drawdown", "ticker": "%", "params": {"window": 5}}, "rhs": {"constant": 5.5}},
            {"condition-type": "binary", "comparator": "lt",
             "lhs": {"fn": "current-price", "ticker": "KMLM", "params": {}},
             "rhs": {"fn": "moving-average-price", "ticker": "KMLM", "params": {"window": 20}}},
        ],
    }  # fmt: skip
    s, _ = import_text(_composer(cond))
    c = s.children[0].condition
    assert len(c.any) == 3
    assert c.any[0].rhs.ticker == "KMLM" and c.any[0].rhs.window == 10
    assert c.any[1].lhs.ticker == "VIXY" and c.any[1].rhs_value == 5.5  # one ticker -> plain comparison
    assert c.any[2].lhs.fn == "current-price" and c.any[2].rhs.window == 20


def test_structured_condition_wins_over_stale_legacy_fields():
    cond = {"condition-type": "binary", "comparator": "gt",
            "lhs": {"fn": "relative-strength-index", "ticker": "VIXY", "params": {"window": 10}}, "rhs": {"constant": 70}}  # fmt: skip
    legacy = {"lhs-fn": "max-drawdown", "lhs-val": "VIXY", "lhs-fn-params": {"window": 5}, "comparator": "gt",
              "rhs-fixed-value?": True, "rhs-val": "5.5"}  # fmt: skip
    s, _ = import_text(_composer(cond, legacy))
    c = s.children[0].condition
    assert c.lhs.fn == "relative-strength-index" and c.rhs_value == 70


def test_unsupported_structured_parts_are_reported():
    bad = {"condition-type": "ternary", "operator": "any"}
    with pytest.raises(ComposerImportError, match="unsupported condition type 'ternary'"):
        import_text(_composer(bad))
    with pytest.raises(ComposerImportError, match="unsupported indicator 'alpha'"):
        import_text(_composer({"condition-type": "binary", "comparator": "gt",
                               "lhs": {"fn": "alpha", "ticker": "SPY"}, "rhs": {"constant": 1}}))  # fmt: skip
