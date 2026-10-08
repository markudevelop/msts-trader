"""Studio order type: market vs market-on-close per strategy.

Regression for scheduled runs failing at the default 15:50 ET: a strategy
inherited `moc = true` from config.toml (the Studio had no way to say
otherwise, and `--moc` had no negative form), and the rebalance engine then
refused MOC with fewer than 12 minutes to the close.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

import msts_trader.__main__ as cli
from msts_trader.__main__ import main
from msts_trader.market_hours import ET
from msts_trader.symphony import runner
from msts_trader.ui import scheduler as sched
from tests.test_studio import H, client, make_sym, studio_home  # noqa: F401  (fixtures)


# ── model ────────────────────────────────────────────────────────────────
def test_order_type_defaults_to_market():
    assert make_sym().deploy.order_type == "market"


def test_moc_rejected_on_broker_without_moc():
    with pytest.raises(ValidationError, match="market-on-close"):
        make_sym(broker="tastytrade", order_type="moc")
    assert make_sym(broker="alpaca", order_type="moc").deploy.order_type == "moc"


# ── runner: order type always explicit ───────────────────────────────────
def test_rebalance_cmd_passes_no_moc_for_market():
    cmd = runner.rebalance_cmd(make_sym(), "w.csv", mode=runner.DRY)
    assert "--no-moc" in cmd and "--moc" not in cmd


def test_rebalance_cmd_passes_moc_for_moc():
    cmd = runner.rebalance_cmd(make_sym(order_type="moc"), "w.csv", mode=runner.DRY)
    assert "--moc" in cmd and "--no-moc" not in cmd


def test_rebalance_cmd_order_type_always_explicit():
    market = runner.rebalance_cmd(make_sym(), "w.csv", mode=runner.DRY)
    chase = runner.rebalance_cmd(make_sym(order_type="limit-chase"), "w.csv", mode=runner.DRY)
    for cmd, kind in ((market, "market"), (chase, "limit-chase")):
        assert cmd[cmd.index("--order-type") + 1] == kind
        assert "--no-moc" in cmd and "--no-extended-hours" in cmd and "--chase-fallback" in cmd
        assert "--fractional" in cmd  # chase legs aren't forced to whole shares


def test_rebalance_cmd_extended_hours():
    cmd = runner.rebalance_cmd(make_sym(order_type="extended"), "w.csv", mode=runner.DRY)
    assert cmd[cmd.index("--order-type") + 1] == "limit-chase"
    assert "--extended-hours" in cmd and "--no-extended-hours" not in cmd


def test_rebalance_cmd_passes_execution_options():
    default = runner.rebalance_cmd(make_sym(), "w.csv", mode=runner.DRY)
    assert default[default.index("--min-weight") + 1] == "0"
    assert default[default.index("--rebalance-scope") + 1] == "whole-book"
    assert "--max-notional" not in default and "--chase-retries" not in default

    s = make_sym(
        order_type="limit-chase",
        rebalance_scope="per-ticker",
        min_weight=0.01,
        whole_shares=True,
        max_notional=25000,
        chase_retries=8,
        chase_interval=3,
        chase_aggression=0.001,
    )
    cmd = runner.rebalance_cmd(s, "w.csv", mode=runner.DRY)
    flag = lambda f: cmd[cmd.index(f) + 1]  # noqa: E731
    assert flag("--rebalance-scope") == "per-ticker" and flag("--min-weight") == "0.01"
    assert "--whole-shares" in cmd and "--fractional" not in cmd
    assert flag("--max-notional") == "25000.0"
    assert (flag("--chase-retries"), flag("--chase-interval"), flag("--chase-aggression")) == ("8", "3.0", "0.001")


def test_rebalance_cmd_options_parse_in_the_cli():
    """Every flag Studio builds must exist on `rebalance` (catches a typo that
    would fail every scheduled run)."""
    from msts_trader.__main__ import rebalance

    known = {o for p in rebalance.params for o in (*p.opts, *p.secondary_opts)}
    s = make_sym(order_type="extended", max_notional=1000, chase_retries=2, chase_interval=1, chase_aggression=0.001)
    sent = {a for a in runner.rebalance_cmd(s, "w.csv", mode=runner.LIVE) if a.startswith("--")}
    assert sent - known == set()


def test_extended_rejected_on_broker_without_it():
    with pytest.raises(ValidationError, match="extended-hours"):
        make_sym(broker="hyperliquid", order_type="extended")


def test_extended_run_time_window():
    tue = datetime(2026, 9, 29).date()
    pre = make_sym(schedule_enabled=True, schedule_time="07:00", order_type="extended")
    late = make_sym(schedule_enabled=True, schedule_time="19:58", order_type="extended")
    early = make_sym(schedule_enabled=True, schedule_time="02:00", order_type="extended")
    assert sched.run_time(pre, tue).strftime("%H:%M") == "07:00"
    assert sched.run_time(late, tue).strftime("%H:%M") == "19:50"
    assert sched.run_time(early, tue).strftime("%H:%M") == "04:00"
    # after-hours run is due after the regular close; a market one isn't
    ah = make_sym(schedule_enabled=True, schedule_time="17:00", order_type="extended")
    assert sched.due(ah, datetime(2026, 9, 29, 17, 1, tzinfo=ET))
    assert not sched.due(ah, datetime(2026, 9, 29, 20, 0, tzinfo=ET))
    assert not sched.due(
        make_sym(schedule_enabled=True, schedule_time="15:50"), datetime(2026, 9, 29, 17, 1, tzinfo=ET)
    )


def test_limit_chase_allowed_on_any_broker():
    assert make_sym(broker="tastytrade", order_type="limit-chase").deploy.order_type == "limit-chase"


def test_limit_chase_run_time_pulled_earlier():
    s = make_sym(schedule_enabled=True, schedule_time="15:50", order_type="limit-chase")
    assert sched.run_time(s, datetime(2026, 9, 29).date()).strftime("%H:%M") == "15:45"
    assert sched.run_time(s, datetime(2026, 11, 27).date()).strftime("%H:%M") == "12:45"


# ── scheduler: MOC runs early enough for the exchange cutoff ─────────────
def test_moc_run_time_pulled_before_cutoff():
    tue = datetime(2026, 9, 29).date()
    market = make_sym(schedule_enabled=True, schedule_time="15:50")
    moc = make_sym(schedule_enabled=True, schedule_time="15:50", order_type="moc")
    assert sched.run_time(market, tue).strftime("%H:%M") == "15:50"
    assert sched.run_time(moc, tue).strftime("%H:%M") == "15:45"
    # an earlier choice is kept
    early = make_sym(schedule_enabled=True, schedule_time="15:30", order_type="moc")
    assert sched.run_time(early, tue).strftime("%H:%M") == "15:30"
    # half-day (13:00 close): 12:45 for MOC, 12:50 for market
    half = datetime(2026, 11, 27).date()
    assert sched.run_time(moc, half).strftime("%H:%M") == "12:45"
    assert sched.run_time(market, half).strftime("%H:%M") == "12:50"


def test_moc_strategy_due_at_its_pulled_time():
    s = make_sym(schedule_enabled=True, schedule_time="15:50", order_type="moc")
    assert sched.due(s, datetime(2026, 9, 29, 15, 46, tzinfo=ET))
    assert not sched.due(s, datetime(2026, 9, 29, 15, 44, tzinfo=ET))


# ── CLI: --no-moc overrides config.toml ──────────────────────────────────
class _Stub:
    name = "paper"
    account_id = "ACC"
    supports_moc = True
    supports_fractional = True


def _near_close(monkeypatch):
    monkeypatch.setattr(cli, "_load_broker", lambda name: _Stub())
    monkeypatch.setattr(
        cli, "market_status", lambda: type("MS", (), {"status": "open", "next_open": None, "minutes_to_close": 9})()
    )


def test_config_moc_is_refused_near_close(tmp_path, monkeypatch):
    # The bug as reported: config says MOC, the run happens 9 min before the close.
    _near_close(monkeypatch)
    cfg = tmp_path / "config.toml"
    cfg.write_text("moc = true\n")
    r = CliRunner().invoke(
        main, ["--broker", "paper", "rebalance", "--config", str(cfg), "--yes"], input="ticker,weight\nSPY,1.0\n"
    )
    assert r.exit_code != 0 and "stop accepting moc" in r.output.lower()


def test_no_moc_overrides_config_moc(tmp_path, monkeypatch):
    _near_close(monkeypatch)
    cfg = tmp_path / "config.toml"
    cfg.write_text("moc = true\n")
    r = CliRunner().invoke(
        main,
        ["--broker", "paper", "rebalance", "--config", str(cfg), "--no-moc", "--yes"],
        input="ticker,weight\nSPY,1.0\n",
    )
    # The stub broker has no balances, so the run fails later — but never on MOC.
    assert "stop accepting moc" not in r.output.lower()


# ── API ──────────────────────────────────────────────────────────────────
def test_meta_exposes_moc_brokers(client):  # noqa: F811
    m = client.get("/api/meta", headers=H()).json()
    assert set(m["moc_brokers"]) == {"alpaca", "ibkr", "schwab", "paper"}
    assert m["moc_lead_minutes"] == 15
