"""Extended-hours weight execution: real request builders, mocked broker I/O."""

from datetime import datetime, timezone
from decimal import Decimal as D
from importlib import import_module
from types import SimpleNamespace as NS

import pytest
from click.testing import CliRunner

from msts_trader import __main__ as m, market_hours as hours, runstate, sleeves
from msts_trader.brokers.paper import Paper
from msts_trader.chase import ChaseConfig, chase_fill
from msts_trader.models import Order, Side, Target
from tests.test_chase import FakeBroker, NOSLEEP


def order(**kw):
    return Order("SPY", Side.BUY, D("10"), extended_hours=True, **kw)


@pytest.fixture(autouse=True)
def isolated_run(monkeypatch, tmp_path):
    monkeypatch.setattr(hours, "now_et", lambda: datetime(2026, 9, 8, 8, 0, tzinfo=hours.ET))
    monkeypatch.setattr(runstate, "already_done", lambda *a: False)
    monkeypatch.setattr(runstate, "record", lambda *a: None)
    monkeypatch.setattr(sleeves, "LEDGER_DIR", tmp_path / "sleeves")
    monkeypatch.setattr(m, "_load_config_or_exit", lambda *a: {})
    monkeypatch.setattr(m, "_do_notify", lambda *a, **kw: None)
    monkeypatch.setattr(m, "_QUIET", False)
    monkeypatch.setattr(m, "_JSON", False)


@pytest.mark.parametrize("session", ["premarket", "afterhours", "open"])
def test_opt_in_sessions(session):
    status = hours.MarketStatus(session, None, None)
    assert hours.trading_session_error("alpaca", extended_hours=True, status=status) is None
    if session != "open":
        assert "--extended-hours" in hours.trading_session_error("alpaca", status=status)


@pytest.mark.parametrize("when", [(9, 8, 3, 59), (9, 8, 20, 0), (9, 7, 8, 0), (9, 12, 8, 0)])
def test_extended_hours_still_blocks_overnight_weekends_holidays(when):
    status = hours.market_status(datetime(2026, *when, tzinfo=hours.ET))
    assert "Market closed" in hours.trading_session_error("alpaca", extended_hours=True, status=status)


def test_clock_normalizes_utc_and_early_close():
    assert hours.market_status(datetime(2026, 9, 8, 12, tzinfo=timezone.utc)).status == "premarket"
    assert hours.market_status(datetime(2026, 11, 27, 13, tzinfo=hours.ET)).status == "afterhours"


@pytest.mark.parametrize(
    "kw, expected",
    [
        ({}, "error"),
        ({"quote_none": True}, "error"),
        ({"partials": {1: 3}}, "PARTIAL"),
        ({"fill_attempt": 1}, "FILLED"),
        ({"cancel_ok": False}, "error"),
    ],
)
def test_chase_never_market_fallback_even_if_config_requests_it(kw, expected):
    broker = FakeBroker(**kw)
    result = chase_fill(broker, order(), ChaseConfig(retries=1), sleep=NOSLEEP)
    assert result["status"] == expected
    assert not broker.market_calls
    if kw.get("partials"):
        assert result["quantity"] == 3
    if not kw:
        assert len(broker.cancelled) == 1
    if kw.get("cancel_ok") is False:
        assert result["order_live"]


def test_chase_preserves_extended_flag_and_partial_remainder():
    broker = FakeBroker(partials={1: 3}, fill_attempt=2)
    original = broker.place_limit
    submitted = []

    def place(o, *a, **kw):
        submitted.append(o)
        return original(o, *a, **kw)

    broker.place_limit = place
    result = chase_fill(broker, order(), ChaseConfig(retries=2), sleep=NOSLEEP)
    assert result["status"] == "FILLED"
    assert [o.quantity for o in submitted] == [D(10), D(7)]
    assert all(o.extended_hours for o in submitted)
    assert not broker.market_calls


def test_chase_stops_when_session_closes(monkeypatch):
    broker = FakeBroker()

    def close_session(*a):
        monkeypatch.setattr(hours, "now_et", lambda: datetime(2026, 9, 8, 20, tzinfo=hours.ET))

    result = chase_fill(broker, order(), ChaseConfig(retries=2), sleep=close_session)
    assert len(broker.placed) == len(broker.cancelled) == 1
    assert "Market closed" in result["reason"]
    assert not broker.market_calls


def test_session_closes_while_fetching_quote(monkeypatch):
    broker = FakeBroker()

    def quote(*a):
        monkeypatch.setattr(hours, "now_et", lambda: datetime(2026, 9, 8, 20, tzinfo=hours.ET))
        return {"SPY": D(100)}

    broker.quote = quote
    result = chase_fill(broker, order(), ChaseConfig(), sleep=NOSLEEP)
    assert "Market closed" in result["reason"]
    assert not broker.placed and not broker.market_calls


@pytest.mark.parametrize(
    "response",
    [
        {"status": "rejected", "reason": "session unavailable"},
        {"status": "error", "reason": "timeout", "order_live": True},
    ],
)
def test_chase_rejection_or_uncertain_submit_never_falls_back(response):
    broker = FakeBroker()
    broker.place_limit = lambda *a, **kw: response.copy()
    result = chase_fill(broker, order(), ChaseConfig(), sleep=NOSLEEP)
    assert not broker.market_calls
    assert response["reason"] in result["reason"]
    assert bool(result.get("order_live")) == bool(response.get("order_live"))


def test_chase_dry_run_never_submits_or_promises_market():
    broker = FakeBroker(quote_none=True)
    result = chase_fill(broker, order(), ChaseConfig(), dry_run=True)
    assert "fallback disabled" in result["reason"]
    assert not broker.placed and not broker.market_calls


@pytest.mark.parametrize(
    "name, cls_name",
    [
        ("alpaca", "Alpaca"),
        ("tastytrade", "Tastytrade"),
        ("ibkr", "IBKR"),
        ("schwab", "Schwab"),
        ("tradier", "Tradier"),
        ("paper", "Paper"),
        ("hyperliquid", "Hyperliquid"),
    ],
)
def test_market_adapters_reject_extended_intent(name, cls_name):
    module = pytest.importorskip(f"msts_trader.brokers.{name}")
    cls = getattr(module, cls_name)
    result = cls.__new__(cls).place_market(order())
    assert result["status"] == "error"
    assert "LIMIT" in result["reason"]


@pytest.mark.parametrize("extended", [True, False])
@pytest.mark.parametrize(
    "name, cls_name",
    [
        ("alpaca", "Alpaca"),
        ("tastytrade", "Tastytrade"),
        ("ibkr", "IBKR"),
        ("schwab", "Schwab"),
    ],
)
def test_real_broker_limit_request_fields(name, cls_name, extended):
    module = pytest.importorskip(f"msts_trader.brokers.{name}")
    cls = getattr(module, cls_name)
    broker = cls.__new__(cls)
    broker.account_id = "test-account"
    captured = []
    resp = NS(status="Submitted", id="1")
    if name == "alpaca":
        broker._client = NS(submit_order=lambda req: captured.append(req) or resp)
    elif name == "tastytrade":
        broker._sess = None
        broker.positions = lambda: {}
        broker._acct = NS(place_order=lambda sess, req, **kw: captured.append(req) or NS(order=resp))
    elif name == "ibkr":
        broker._ib = NS(
            qualifyContracts=lambda *a: None,
            sleep=NOSLEEP,
            placeOrder=lambda ct, req: (
                captured.append(req) or NS(order=NS(permId=1), orderStatus=NS(status="Submitted"), log=[])
            ),
        )
    else:
        broker._account_hash = "test-hash"
        broker._client = NS(
            place_order=lambda account, req: (
                captured.append(req) or NS(headers={"Location": "/orders/1"}, raise_for_status=NOSLEEP)
            )
        )
    result = broker.place_limit(Order("SPY", Side.BUY, D(10), extended_hours=extended), D("100.25"))
    assert result["order_id"] == "1"
    req = captured[0]
    if name == "alpaca":
        assert req.extended_hours is extended
        assert req.type.value == "limit" and req.time_in_force.value == "day"
    elif name == "tastytrade":
        assert req.time_in_force.value == ("Ext" if extended else "Day")
        assert req.order_type.value == "Limit" and abs(req.price) == D("100.25")
    elif name == "ibkr":
        assert req.outsideRth is extended
        assert req.orderType == "LMT" and req.tif == "DAY"
    else:
        assert req["session"] == ("SEAMLESS" if extended else "NORMAL")
        assert req["orderType"] == "LIMIT" and req["duration"] == "DAY"


@pytest.mark.parametrize(
    "hour, minute, duration",
    [(8, 0, "pre"), (10, 0, "day"), (17, 0, "post"), (6, 59, None), (9, 24, None), (19, 55, None)],
)
def test_tradier_session_duration_and_cutoffs(monkeypatch, hour, minute, duration):
    from msts_trader.brokers.tradier import Tradier

    monkeypatch.setattr(hours, "now_et", lambda: datetime(2026, 9, 8, hour, minute, tzinfo=hours.ET))
    broker = Tradier.__new__(Tradier)
    broker.account_id = "test"
    requests = []
    broker._request = lambda method, path, params: requests.append(params) or {"order": {"id": 1}}
    result = broker.place_limit(order(), D(100))
    if duration:
        assert requests[0]["duration"] == duration
        assert requests[0]["type"] == "limit"
    else:
        assert not requests and result["status"] == "error"


def paper_as_equity(monkeypatch, name="alpaca"):
    broker = Paper()
    broker.name = name
    broker.set_quote("SPY", D(99))
    monkeypatch.setattr(m, "_load_broker", lambda *a: broker)
    return broker


@pytest.mark.parametrize("hour", [8, 17])
def test_cli_executes_weights_with_extended_flag(monkeypatch, hour):
    monkeypatch.setattr(hours, "now_et", lambda: datetime(2026, 9, 8, hour, tzinfo=hours.ET))
    broker = paper_as_equity(monkeypatch)
    original = broker.place_limit
    orders = []
    broker.place_limit = lambda o, *a, **kw: orders.append(o) or original(o, *a, **kw)
    broker.place_market = lambda *a, **kw: pytest.fail("market order submitted")
    result = CliRunner().invoke(
        m.main,
        ["rebalance", "--broker", "alpaca", "--extended-hours", "--yes", "--no-verify", "--chase-poll", "0"],
        input="ticker,weight\nSPY,0.5\n",
    )
    assert result.exit_code == 0, result.output
    assert orders and all(o.extended_hours for o in orders)
    assert broker.positions()["SPY"].quantity > 0


def test_cli_rejects_extended_session_without_opt_in():
    result = CliRunner().invoke(m.main, ["rebalance", "--broker", "alpaca", "--yes"], input="ticker,weight\nSPY,1\n")
    assert result.exit_code == 2 and "--extended-hours" in result.output


@pytest.mark.parametrize("extra", [["--moc"], ["--sleeve", "weights"]])
def test_cli_rejects_incompatible_modes(extra):
    result = CliRunner().invoke(m.main, ["rebalance", "--extended-hours", *extra, "--dry-run"])
    assert result.exit_code == 1


def test_cli_dry_run_config_and_whole_share_preview(monkeypatch):
    import json

    broker = paper_as_equity(monkeypatch, "tastytrade")
    broker.place_limit = lambda *a, **kw: pytest.fail("dry-run submitted")
    monkeypatch.setattr(m, "_load_config_or_exit", lambda *a: {"extended_hours": True})
    result = CliRunner().invoke(
        m.main, ["rebalance", "--broker", "tastytrade", "--dry-run", "--json"], input="ticker,weight\nSPY,0.5\n"
    )
    assert result.exit_code == 0, result.output
    planned = json.loads(result.output)["orders"][0]
    assert planned["extended_hours"]
    assert D(planned["quantity"]) == D(planned["quantity"]).to_integral_value()
    result = CliRunner().invoke(
        m.main,
        ["rebalance", "--broker", "tastytrade", "--no-extended-hours", "--yes"],
        input="ticker,weight\nSPY,0.5\n",
    )
    assert result.exit_code == 2


def test_multi_preflight_blocks_closed_and_allows_opt_in(monkeypatch):
    broker = paper_as_equity(monkeypatch)
    args = dict(threshold=0, max_notional=None, dry_run=False, force=True)
    targets = [Target("SPY", D("0.5"))]
    assert m._rebalance_one(broker, targets, **args)["status"] == "blocked"
    broker.place_market = lambda *a, **kw: pytest.fail("market order submitted")
    result = m._rebalance_one(broker, targets, extended_hours=True, chase_cfg=ChaseConfig(poll_interval=0), **args)
    assert result["status"] == "executed"
    monkeypatch.setattr(hours, "now_et", lambda: datetime(2026, 9, 8, 20, tzinfo=hours.ET))
    assert m._rebalance_one(broker, targets, extended_hours=True, **args)["status"] == "blocked"


def test_multi_config_override_preserves_chase_settings(monkeypatch, tmp_path):
    csv = tmp_path / "weights.csv"
    csv.write_text("ticker,weight\nSPY,0.5\n")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'csv_file = "{csv.as_posix()}"\nextended_hours = true\nchase_retries = 9\n'
        '[[account]]\nbroker = "paper"\n[[account]]\nbroker = "paper"\nextended_hours = false\n'
    )
    monkeypatch.setattr(m, "_load_config_or_exit", lambda path: import_module("msts_trader.config").load(path))
    monkeypatch.setattr(m, "make", lambda *a, **kw: NS(name="paper", account_id="test"))
    monkeypatch.setattr(m, "broker_kwargs_from_env", lambda *a: {})
    seen = []
    monkeypatch.setattr(m, "_rebalance_one", lambda *a, **kw: seen.append(kw) or {"status": "dry-run"})
    runner = CliRunner()
    result = runner.invoke(m.main, ["multi", "--config", str(cfg), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert [r["extended_hours"] for r in seen] == [True, False]
    assert seen[0]["chase_cfg"].retries == 9
    seen.clear()
    result = runner.invoke(m.main, ["multi", "--config", str(cfg), "--dry-run", "--no-extended-hours"])
    assert result.exit_code == 0, result.output
    assert not any(r["extended_hours"] for r in seen)


@pytest.mark.parametrize("session, expected", [("premarket", 1), ("afterhours", 1), ("open", 1), ("closed", 0)])
def test_self_heal_preserves_extended_execution(monkeypatch, session, expected):
    post = NS(orders=[Order("SPY", Side.BUY, D(5))], warnings=[])
    result = NS(ok=False, summary=lambda: "residual")
    monkeypatch.setattr(m, "_verify_once", lambda *a, **kw: (result, post))
    monkeypatch.setattr(m, "market_status", lambda: hours.MarketStatus(session, None, None))
    submitted = []
    monkeypatch.setattr(m, "_execute", lambda b, p, **kw: submitted.extend(p.orders) or (0, 1, []))
    m._post_trade_verify(
        NS(name="alpaca", account_id="test"), [], extended_hours=True, self_heal=True, settle_seconds=0
    )
    assert len(submitted) == expected
    assert all(o.extended_hours for o in submitted)


def test_execute_rechecks_session_before_cancelling_stops(monkeypatch):
    broker = paper_as_equity(monkeypatch)
    broker.open_stops = lambda: pytest.fail("stops touched after session closed")
    monkeypatch.setattr(hours, "now_et", lambda: datetime(2026, 9, 8, 20, tzinfo=hours.ET))
    sent, failed, results = m._execute(broker, NS(orders=[order()]))
    assert (sent, failed) == (0, 1)
    assert "Market closed" in results[0]["reason"]


@pytest.mark.parametrize("json_out", [False, True])
def test_unfilled_cli_run_exits_nonzero_and_does_not_record_success(monkeypatch, json_out):
    broker = paper_as_equity(monkeypatch)
    broker.place_limit = lambda *a, **kw: {"status": "rejected", "reason": "no liquidity"}
    monkeypatch.setattr(runstate, "record", lambda *a: pytest.fail("failed rebalance marked complete"))
    args = ["rebalance", "--broker", "alpaca", "--extended-hours", "--yes", "--no-verify"]
    if json_out:
        args.append("--json")
    result = CliRunner().invoke(m.main, args, input="ticker,weight\nSPY,0.5\n")
    assert result.exit_code == 1, result.output
    assert "no liquidity" in result.output


def test_unsupported_extended_broker_never_degrades_to_market(monkeypatch):
    broker = paper_as_equity(monkeypatch)
    broker.supports_extended_hours = False
    broker.place_market = lambda *a, **kw: pytest.fail("degraded to market")
    result = CliRunner().invoke(
        m.main, ["rebalance", "--broker", "alpaca", "--extended-hours", "--yes"], input="ticker,weight\nSPY,0.5\n"
    )
    assert result.exit_code == 1 and "does not support extended-hours" in result.output


def test_within_drift_dry_run_never_reconciles_stops(monkeypatch):
    paper_as_equity(monkeypatch)
    monkeypatch.setattr(m, "_reconcile_stops", lambda *a, **kw: pytest.fail("dry-run touched stops"))
    result = CliRunner().invoke(
        m.main, ["rebalance", "--broker", "alpaca", "--extended-hours", "--dry-run"], input="ticker,weight\nSPY,0\n"
    )
    assert result.exit_code == 0, result.output
