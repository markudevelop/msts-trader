"""Sleeve ledger (msts_trader.sleeves) — per-strategy share tallies.

Unit tests for the ledger itself (fills-not-intent, idempotent settlement,
the ≤-invariant helpers) plus end-to-end CLI runs on the paper broker proving
what the feature promises: two sleeves sharing a ticker in one account, plus
manually-traded shares, and nobody touches anyone else's stock.
"""

from __future__ import annotations

import json as _json
from decimal import Decimal
from pathlib import Path

import keyring
import pytest
from click.testing import CliRunner
from keyring.backend import KeyringBackend

from msts_trader import sleeves
from msts_trader.__main__ import main
from msts_trader.models import Position

# ------------------------------------------------------------------- units ---


@pytest.fixture(autouse=True)
def ledger_dir(monkeypatch, tmp_path):
    d = tmp_path / "sleeves"
    monkeypatch.setattr(sleeves, "LEDGER_DIR", d)
    return d


def _ledger() -> sleeves.Ledger:
    return sleeves.Ledger(broker="paper", account="PAPER")


class _FakeBroker:
    """order_status stub: maps order_id -> (status, filled_qty)."""

    name = "paper"
    account_id = "PAPER"

    def __init__(self, statuses: dict):
        self.statuses = statuses

    def order_status(self, oid):
        st = self.statuses[oid]
        if isinstance(st, Exception):
            raise st
        return {"status": st[0], "filled_qty": st[1], "filled_avg_price": None}


def test_round_trip_preserves_decimals_and_writes_backup():
    led = _ledger()
    sleeves.apply_fill(led, "momo", "SPY", "BUY", Decimal("10.55"))
    sleeves.save(led)
    sleeves.save(led)  # second save -> previous version kept as .bak

    back = sleeves.load("paper", "PAPER")
    assert back.tally("momo", "SPY") == Decimal("10.55")
    p = sleeves.LEDGER_DIR / "paper_PAPER.json"
    assert p.exists() and p.with_suffix(".json.bak").exists()


def test_corrupt_ledger_fails_closed(ledger_dir):
    ledger_dir.mkdir(parents=True)
    (ledger_dir / "paper_PAPER.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(sleeves.SleeveError, match="unreadable"):
        sleeves.load("paper", "PAPER")


def test_sell_fill_clamps_at_zero():
    led = _ledger()
    sleeves.apply_fill(led, "momo", "SPY", "BUY", Decimal("5"))
    sleeves.apply_fill(led, "momo", "SPY", "SELL", Decimal("9"))  # broker over-report
    assert led.tally("momo", "SPY") == Decimal("0")


def test_settlement_is_idempotent_via_recorded_fill_cursor():
    """Partial fill applied once; a re-poll of the same numbers applies nothing;
    more fill applies only the delta; terminal orders leave pending."""
    led = _ledger()
    sleeves.record_order(led, "momo", order_id="o1", ticker="SPY", side="BUY", requested=Decimal("10"))

    b = _FakeBroker({"o1": ("working", 4.0)})
    sleeves.settle_pending(led, b)
    assert led.tally("momo", "SPY") == Decimal("4")
    sleeves.settle_pending(led, b)  # same numbers again — must NOT double-count
    assert led.tally("momo", "SPY") == Decimal("4")
    assert len(led.pending) == 1  # still live

    b.statuses["o1"] = ("filled", 10.0)
    sleeves.settle_pending(led, b)
    assert led.tally("momo", "SPY") == Decimal("10")
    assert led.pending == []  # terminal -> dropped


def test_settlement_caps_fill_at_requested_and_keeps_unreadable_orders():
    led = _ledger()
    sleeves.record_order(led, "momo", order_id="o1", ticker="SPY", side="BUY", requested=Decimal("10"))
    sleeves.record_order(led, "momo", order_id="o2", ticker="GLD", side="BUY", requested=Decimal("5"))

    b = _FakeBroker({"o1": ("filled", 12.0), "o2": RuntimeError("api down")})
    sleeves.settle_pending(led, b)
    assert led.tally("momo", "SPY") == Decimal("10")  # over-report capped
    assert [e["order_id"] for e in led.pending] == ["o2"]  # unreadable -> retry next run


def test_rejected_order_settles_to_nothing():
    led = _ledger()
    sleeves.record_order(led, "momo", order_id="o1", ticker="SPY", side="BUY", requested=Decimal("10"))
    sleeves.settle_pending(led, _FakeBroker({"o1": ("rejected", 0.0)}))
    assert led.tally("momo", "SPY") == Decimal("0")
    assert led.pending == []


def test_negative_residuals_flags_only_overclaims():
    led = _ledger()
    sleeves.apply_fill(led, "momo", "SPY", "BUY", Decimal("40"))
    sleeves.apply_fill(led, "carry", "SPY", "BUY", Decimal("20"))
    sleeves.apply_fill(led, "momo", "GLD", "BUY", Decimal("5"))
    pos = {
        "SPY": Position("SPY", Decimal("55"), Decimal("500")),  # claims 60 > 55 -> short 5
        "GLD": Position("GLD", Decimal("9"), Decimal("200")),  # claims 5 <= 9 -> fine (4 unassigned)
    }
    assert sleeves.negative_residuals(led, pos) == {"SPY": Decimal("-5")}


def test_sleeve_view_shows_only_own_tally_clamped_to_account():
    led = _ledger()
    sleeves.apply_fill(led, "momo", "SPY", "BUY", Decimal("40"))
    sleeves.apply_fill(led, "momo", "EEM", "BUY", Decimal("10"))  # account no longer holds EEM
    sleeves.apply_fill(led, "carry", "SHV", "BUY", Decimal("100"))
    pos = {
        "SPY": Position("SPY", Decimal("50"), Decimal("500")),  # 40 momo + 10 manual
        "SHV": Position("SHV", Decimal("100"), Decimal("110")),
        "AAPL": Position("AAPL", Decimal("400"), Decimal("200")),  # manual only
    }
    view = sleeves.sleeve_view(led, "momo", pos)
    assert set(view) == {"SPY"}  # no EEM (not held), no SHV (carry's), no AAPL (manual)
    assert view["SPY"].quantity == Decimal("40")


def test_adopt_refuses_to_claim_past_unassigned():
    led = _ledger()
    sleeves.apply_fill(led, "carry", "SPY", "BUY", Decimal("30"))
    pos = {"SPY": Position("SPY", Decimal("50"), Decimal("500"))}
    sleeves.adopt(led, "momo", "SPY", Decimal("20"), pos)  # exactly the unassigned 20
    with pytest.raises(sleeves.SleeveError, match="unassigned"):
        sleeves.adopt(led, "momo", "SPY", Decimal("1"), pos)


def test_release_bounds():
    led = _ledger()
    sleeves.apply_fill(led, "momo", "SPY", "BUY", Decimal("10"))
    sleeves.release(led, "momo", "SPY", Decimal("4"))
    assert led.tally("momo", "SPY") == Decimal("6")
    with pytest.raises(sleeves.SleeveError):
        sleeves.release(led, "momo", "SPY", Decimal("7"))


def test_lock_is_acquired_and_released(ledger_dir):
    with sleeves.lock("paper", "PAPER"):
        assert (ledger_dir / "paper_PAPER.json.lock").exists()
    assert not (ledger_dir / "paper_PAPER.json.lock").exists()


# ------------------------------------------------- end-to-end (paper broker) ---


class _MemKeyring(KeyringBackend):
    priority = 100

    def __init__(self):
        self._s = {}

    def get_password(self, service, username):
        return self._s.get((service, username))

    def set_password(self, service, username, password):
        self._s[(service, username)] = password

    def delete_password(self, service, username):
        self._s.pop((service, username), None)


@pytest.fixture
def paper_env(monkeypatch, tmp_path):
    backend = _MemKeyring()
    monkeypatch.setattr(keyring, "get_password", lambda s, u: backend.get_password(s, u))
    monkeypatch.setattr(keyring, "set_password", lambda s, u, p: backend.set_password(s, u, p))
    monkeypatch.setattr(keyring, "delete_password", lambda s, u: backend.delete_password(s, u))
    from msts_trader import fill_log, runstate
    from msts_trader.brokers import paper

    monkeypatch.setattr(paper, "STATE_PATH", tmp_path / "paper.json")
    monkeypatch.setattr(runstate, "STATE_PATH", tmp_path / "runstate.json")
    monkeypatch.setattr(fill_log, "LOG_DIR", tmp_path / "fills")
    runner = CliRunner()
    assert runner.invoke(main, ["login", "--broker", "paper"], input="100000\n").exit_code == 0
    from msts_trader.brokers.paper import Paper
    from msts_trader.models import Order, Side

    p = Paper(starting_cash="100000")
    p.set_quote("SPY", Decimal("500"))
    p.set_quote("SHV", Decimal("110"))
    # The owner's MANUAL trade: 20 SPY bought outside any sleeve.
    r = p.place_market(Order(ticker="SPY", side=Side.BUY, quantity=Decimal("20"), estimated_price=Decimal("500")))
    assert str(r["status"]).lower() in ("filled", "routed"), r
    return runner, tmp_path


def _positions(runner) -> dict[str, Decimal]:
    out = runner.invoke(main, ["--broker", "paper", "status", "--json"])
    payload = _json.loads(out.output.strip().splitlines()[-1])
    return {p["ticker"]: Decimal(p["quantity"]) for p in payload["positions"]}


def _csv(tmp_path: Path, name: str, body: str) -> str:
    f = tmp_path / name
    f.write_text(body, encoding="utf-8")
    return str(f)


def test_two_sleeves_share_a_ticker_and_manual_shares_survive(paper_env, tmp_path):
    """The whole feature in one scenario: manual SPY + two sleeves both holding
    SPY in one paper account. Each sleeve converges on its own allocation and
    NOBODY sells anyone else's shares."""
    runner, tp = paper_env

    momo = _csv(tp, "momo.csv", "ticker,weight\nSPY,1.0\n")
    r = runner.invoke(
        main,
        ["--broker", "paper", "rebalance", "--sleeve", "momo", "--allocation", "20000", "--csv-file", momo, "--yes"],
    )
    assert r.exit_code == 0, r.output
    held = _positions(runner)
    assert held["SPY"] == Decimal("60")  # 20 manual + 40 momo (20000/500)

    led = sleeves.load("paper", "PAPER")
    assert led.tally("momo", "SPY") == Decimal("40")
    assert led.pending == []  # paper fills instantly -> settled

    # Sleeve B buys SPY too — must NOT see momo's or the manual shares.
    carry = _csv(tp, "carry.csv", "ticker,weight\nSPY,0.5\nSHV,0.5\n")
    r = runner.invoke(
        main,
        ["--broker", "paper", "rebalance", "--sleeve", "carry", "--allocation", "10000", "--csv-file", carry, "--yes"],
    )
    assert r.exit_code == 0, r.output
    held = _positions(runner)
    assert held["SPY"] == Decimal("70")  # +10 carry (5000/500)
    led = sleeves.load("paper", "PAPER")
    assert led.tally("carry", "SPY") == Decimal("10")
    assert led.tally("momo", "SPY") == Decimal("40")  # untouched by carry's run

    # Re-run momo with the SAME plan forced: within drift -> no orders, and
    # crucially it does NOT sweep-sell the manual or carry shares it can't see.
    r = runner.invoke(
        main,
        [
            "--broker",
            "paper",
            "rebalance",
            "--sleeve",
            "momo",
            "--allocation",
            "20000",
            "--csv-file",
            momo,
            "--yes",
            "--force",
        ],
    )
    assert r.exit_code == 0, r.output
    assert "nothing to do" in r.output.lower()
    assert _positions(runner)["SPY"] == Decimal("70")


def test_sleeve_rotation_sells_only_its_own_shares(paper_env, tmp_path):
    """momo rotates SPY -> SHV. It owns 40 of the account's 60 SPY; the sell
    must be exactly 40, leaving the 20 manual shares."""
    runner, tp = paper_env
    momo = _csv(tp, "momo.csv", "ticker,weight\nSPY,1.0\n")
    r = runner.invoke(
        main,
        [
            "--broker",
            "paper",
            "rebalance",
            "--sleeve",
            "momo",
            "--allocation",
            "20000",
            "--csv-file",
            momo,
            "--yes",
            "--no-verify",
        ],
    )
    assert r.exit_code == 0, r.output

    rot = _csv(tp, "rot.csv", "ticker,weight\nSHV,1.0\n")
    r = runner.invoke(
        main,
        [
            "--broker",
            "paper",
            "rebalance",
            "--sleeve",
            "momo",
            "--allocation",
            "20000",
            "--csv-file",
            rot,
            "--yes",
            "--no-verify",
        ],
    )
    assert r.exit_code == 0, r.output
    held = _positions(runner)
    assert held["SPY"] == Decimal("20")  # manual shares — exactly — survive the sweep
    assert held.get("SHV", Decimal(0)) > 0
    led = sleeves.load("paper", "PAPER")
    assert led.tally("momo", "SPY") == Decimal("0")


def test_manual_sell_of_sleeve_shares_is_refused_fail_closed(paper_env, tmp_path):
    runner, tp = paper_env
    momo = _csv(tp, "momo.csv", "ticker,weight\nSPY,1.0\n")
    r = runner.invoke(
        main,
        [
            "--broker",
            "paper",
            "rebalance",
            "--sleeve",
            "momo",
            "--allocation",
            "20000",
            "--csv-file",
            momo,
            "--yes",
            "--no-verify",
        ],
    )
    assert r.exit_code == 0, r.output

    # Owner manually sells 30 SPY — more than the 20 unassigned shares, so 10
    # of momo's shares are gone. The ledger now over-claims.
    from msts_trader.brokers.paper import Paper
    from msts_trader.models import Order, Side

    p = Paper(starting_cash="100000")
    r2 = p.place_market(Order(ticker="SPY", side=Side.SELL, quantity=Decimal("30"), estimated_price=Decimal("500")))
    assert str(r2["status"]).lower() in ("filled", "routed")

    r3 = runner.invoke(
        main,
        [
            "--broker",
            "paper",
            "rebalance",
            "--sleeve",
            "momo",
            "--allocation",
            "20000",
            "--csv-file",
            momo,
            "--yes",
            "--no-verify",
            "--force",
        ],
    )
    assert r3.exit_code != 0
    assert "claims more shares than the account holds" in r3.output
    assert "reconcile" in r3.output


def test_account_level_run_on_ledgered_account_is_refused(paper_env, tmp_path):
    runner, tp = paper_env
    momo = _csv(tp, "momo.csv", "ticker,weight\nSPY,1.0\n")
    r = runner.invoke(
        main,
        [
            "--broker",
            "paper",
            "rebalance",
            "--sleeve",
            "momo",
            "--allocation",
            "20000",
            "--csv-file",
            momo,
            "--yes",
            "--no-verify",
        ],
    )
    assert r.exit_code == 0, r.output

    r2 = runner.invoke(main, ["--broker", "paper", "rebalance", "--csv-file", momo, "--yes", "--no-verify", "--force"])
    assert r2.exit_code != 0
    assert "sleeve tallies" in r2.output and "--sleeve" in r2.output


def test_sleeve_refuses_stops_and_limit_chase(paper_env, tmp_path):
    runner, tp = paper_env
    stops = _csv(tp, "s.csv", "ticker,weight,stop_pct\nSPY,1.0,0.02\n")
    r = runner.invoke(main, ["--broker", "paper", "rebalance", "--sleeve", "momo", "--csv-file", stops, "--dry-run"])
    assert r.exit_code != 0 and "stop" in r.output.lower()

    plain = _csv(tp, "p.csv", "ticker,weight\nSPY,1.0\n")
    r2 = runner.invoke(
        main,
        [
            "--broker",
            "paper",
            "rebalance",
            "--sleeve",
            "momo",
            "--csv-file",
            plain,
            "--order-type",
            "limit-chase",
            "--dry-run",
        ],
    )
    assert r2.exit_code != 0 and "market orders only" in r2.output

    r3 = runner.invoke(
        main,
        ["--broker", "paper", "rebalance", "--sleeve", "momo", "--csv-file", plain, "--stop-pct", "0.02", "--dry-run"],
    )
    assert r3.exit_code != 0 and "stop" in r3.output.lower()


def test_sleeve_adopt_and_reconcile_cli(paper_env, tmp_path):
    runner, tp = paper_env
    # Adopt 15 of the 20 manually-bought SPY into a sleeve.
    r = runner.invoke(main, ["sleeve", "adopt", "legacy", "SPY", "15", "--broker", "paper"])
    assert r.exit_code == 0, r.output
    assert sleeves.load("paper", "PAPER").tally("legacy", "SPY") == Decimal("15")

    # Over-adopting the remaining 5 unassigned is refused.
    r2 = runner.invoke(main, ["sleeve", "adopt", "legacy", "SPY", "6", "--broker", "paper"])
    assert r2.exit_code != 0 and "unassigned" in r2.output

    r3 = runner.invoke(main, ["sleeve", "reconcile", "--broker", "paper"])
    assert r3.exit_code == 0, r3.output
    assert "consistent" in r3.output

    # Break it (claim shares the account doesn't hold) -> reconcile flags it.
    r4 = runner.invoke(main, ["sleeve", "adjust", "legacy", "SPY", "99", "--broker", "paper"])
    assert r4.exit_code == 0
    r5 = runner.invoke(main, ["sleeve", "reconcile", "--broker", "paper"])
    assert r5.exit_code != 0 and "more than the account holds" in r5.output


def test_sleeve_fingerprint_is_distinct_per_sleeve(paper_env, tmp_path):
    """Two different sleeves running IDENTICAL targets the same day must not
    suppress each other as duplicates."""
    runner, tp = paper_env
    csv = _csv(tp, "t.csv", "ticker,weight\nSPY,1.0\n")
    r = runner.invoke(
        main,
        [
            "--broker",
            "paper",
            "rebalance",
            "--sleeve",
            "a",
            "--allocation",
            "10000",
            "--csv-file",
            csv,
            "--yes",
            "--no-verify",
        ],
    )
    assert r.exit_code == 0, r.output
    r2 = runner.invoke(
        main,
        [
            "--broker",
            "paper",
            "rebalance",
            "--sleeve",
            "b",
            "--allocation",
            "10000",
            "--csv-file",
            csv,
            "--yes",
            "--no-verify",
        ],
    )
    assert r2.exit_code == 0, r2.output
    assert "already executed today" not in r2.output
    led = sleeves.load("paper", "PAPER")
    assert led.tally("a", "SPY") == Decimal("20") and led.tally("b", "SPY") == Decimal("20")


# ------------------------------------------------ __main__ sleeve plumbing ---


class _PlumbBroker:
    """Broker stub for the __main__-level helpers: order_status + the
    balances/positions/quote surface _verify_once fetches."""

    name = "paper"
    account_id = "PAPER"
    supports_stops = False

    def __init__(self, statuses=None, positions=None, nav="100000"):
        self.statuses = statuses or {}
        self._positions = positions or {}
        self._nav = Decimal(nav)

    def order_status(self, oid):
        st = self.statuses[oid]
        if isinstance(st, Exception):
            raise st
        return {"status": st[0], "filled_qty": st[1], "filled_avg_price": None}

    def balances(self):
        from msts_trader.brokers.base import Balances

        return Balances(nav=self._nav, cash=Decimal("0"), buying_power=self._nav)

    def positions(self):
        return dict(self._positions)

    def quote(self, tickers):
        return {t: Decimal("500") for t in tickers}


def test_record_sleeve_fills_records_only_placed_orders_and_settles():
    """The executor hand-off: results without an order_id (never reached the
    broker) leave no trace; filled orders land in the tally; a resting order
    stays pending with its partial fill recorded; and the ledger is saved."""
    from msts_trader import __main__ as m
    from msts_trader.models import Order, Side

    led = sleeves.Ledger(broker="paper", account="PAPER")
    orders = [
        Order(ticker="SPY", side=Side.BUY, quantity=Decimal("10")),
        Order(ticker="GLD", side=Side.BUY, quantity=Decimal("5")),
        Order(ticker="SHV", side=Side.BUY, quantity=Decimal("3")),
    ]
    results = [
        {"order_id": "a1", "status": "FILLED", "ticker": "SPY"},
        {"status": "error", "reason": "no price", "ticker": "GLD"},  # no id
        {"order_id": "a3", "status": "resting", "ticker": "SHV"},
    ]
    b = _PlumbBroker(statuses={"a1": ("filled", 10.0), "a3": ("working", 1.0)})

    m._record_sleeve_fills(b, led, "momo", orders, results)

    assert led.tally("momo", "SPY") == Decimal("10")
    assert led.tally("momo", "GLD") == Decimal("0")  # errored -> no entry at all
    assert led.tally("momo", "SHV") == Decimal("1")  # partial applied
    assert [e["order_id"] for e in led.pending] == ["a3"]  # resting -> next run
    # Persisted, not just in memory.
    assert sleeves.load("paper", "PAPER").tally("momo", "SPY") == Decimal("10")


def test_verify_once_scopes_convergence_to_the_sleeve():
    """Account holds 60 SPY (40 sleeve + 20 manual). Sleeve targets $20k SPY
    at $500 = 40 shares: sleeve-scoped verify converges; the same verify
    WITHOUT the sleeve view sees all 60 and reports a residual sell — proving
    the sleeve param changes the outcome, not just the plumbing."""
    from msts_trader import __main__ as m
    from msts_trader.models import Target

    led = sleeves.Ledger(broker="paper", account="PAPER")
    sleeves.apply_fill(led, "momo", "SPY", "BUY", Decimal("40"))
    b = _PlumbBroker(positions={"SPY": Position("SPY", Decimal("60"), Decimal("500"))})
    targets = [Target(ticker="SPY", weight=Decimal("1.0"))]

    res_sleeve, post = m._verify_once(
        b,
        targets,
        threshold=0.04,
        min_weight=None,
        allocation=Decimal("20000"),
        whole_shares=False,
        threshold_mode="nav",
        sleeve="momo",
        ledger=led,
    )
    assert res_sleeve.ok, [r.ticker for r in res_sleeve.residual]
    assert post.orders == []

    res_account, post_account = m._verify_once(
        b,
        targets,
        threshold=0.04,
        min_weight=None,
        allocation=Decimal("20000"),
        whole_shares=False,
        threshold_mode="nav",
    )
    assert not res_account.ok  # sees the manual shares as overweight
    assert any(o.side.value == "SELL" for o in post_account.orders)


def test_self_heal_records_healed_fills_into_the_ledger(monkeypatch):
    """A heal pass goes through _execute like any trade — its fills MUST land
    in the tally, or the next verify double-counts the residual."""
    import types

    from msts_trader import __main__ as m
    from msts_trader.models import Order, Side

    led = sleeves.Ledger(broker="paper", account="PAPER")
    b = _PlumbBroker(statuses={"h1": ("filled", 7.0)})
    heal_order = Order(ticker="SPY", side=Side.BUY, quantity=Decimal("7"))

    class _Res:
        def __init__(self, ok):
            self.ok = ok
            self.residual = [] if ok else [object()]

        def summary(self):
            return "x"

    seq = [(_Res(False), types.SimpleNamespace(orders=[heal_order])), (_Res(True), types.SimpleNamespace(orders=[]))]
    calls = {"n": 0}

    def fake_verify(*a, **k):
        r = seq[min(calls["n"], 1)]
        calls["n"] += 1
        return r

    monkeypatch.setattr(m, "_verify_once", fake_verify)
    monkeypatch.setattr(m, "market_status", lambda: types.SimpleNamespace(status="open"))
    monkeypatch.setattr(
        m, "_execute", lambda *a, **k: (1, 0, [{"order_id": "h1", "status": "FILLED", "ticker": "SPY"}])
    )
    monkeypatch.setattr(m, "_do_notify", lambda *a, **k: None)
    monkeypatch.setattr(m, "say", lambda *a, **k: None)

    res = m._post_trade_verify(b, [], settle_seconds=0, self_heal=True, heal_passes=1, sleeve="momo", ledger=led)

    assert res.ok is True
    assert led.tally("momo", "SPY") == Decimal("7")
    assert sleeves.load("paper", "PAPER").tally("momo", "SPY") == Decimal("7")
