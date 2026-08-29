"""`examples/merge_sleeves.py` — merging strategy sleeves into one target CSV.

The example is how the README tells people to run several strategies (and their
own manual trades) inside ONE account, so its arithmetic and its `--no-sweep`
behaviour are load-bearing: a wrong weight mis-sizes real orders, and a dropped
exit row strands a real position. The last test runs the merged output through
the shipped parser and diff engine, which is the claim the README actually
makes.
"""

from __future__ import annotations

import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest

from msts_trader.csv_parser import parse_csv
from msts_trader.diff import build_preview
from msts_trader.models import Position, Side

_EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "merge_sleeves.py"


def _load_example():
    spec = importlib.util.spec_from_file_location("merge_sleeves", _EXAMPLE)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


merge_sleeves = _load_example()


def _write(tmp_path: Path, name: str, body: str) -> str:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return str(p)


def _run(monkeypatch, capsys, *argv: str) -> str:
    monkeypatch.setattr("sys.argv", ["merge_sleeves.py", *argv])
    merge_sleeves.main(list(argv))
    return capsys.readouterr().out


def _rows(out: str) -> dict[str, str]:
    lines = out.strip().splitlines()
    return {ln.split(",")[0]: ln.split(",")[1] for ln in lines[1:]}


# ------------------------------------------------------------------- merge ---


def test_shared_ticker_is_summed_across_sleeves(tmp_path):
    """$50k of (SPY .60) + $30k of (SPY .50) = $45k of an $80k book = 0.5625.

    The whole point of merging: msts-trader rejects duplicate ticker rows, and
    the account can only hold one combined position anyway.
    """
    momo = _write(tmp_path, "momo.csv", "ticker,weight\nSPY,0.60\nGLD,0.40\n")
    carry = _write(tmp_path, "carry.csv", "ticker,weight\nSPY,0.50\nSHV,0.50\n")

    weights, _ = merge_sleeves.merge([(Decimal("50000"), momo), (Decimal("30000"), carry)])

    assert weights["SPY"] == Decimal("0.5625")  # (30000 + 15000) / 80000
    assert weights["GLD"] == Decimal("0.25")  # 20000 / 80000
    assert weights["SHV"] == Decimal("0.1875")  # 15000 / 80000
    assert sum(weights.values()) == Decimal("1")


def test_allocations_set_each_sleeve_share_of_the_book(tmp_path):
    # Same weights, different money: the bigger sleeve dominates the merged book.
    a = _write(tmp_path, "a.csv", "ticker,weight\nAAA,1.0\n")
    b = _write(tmp_path, "b.csv", "ticker,weight\nBBB,1.0\n")

    weights, _ = merge_sleeves.merge([(Decimal("90000"), a), (Decimal("10000"), b)])

    assert weights["AAA"] == Decimal("0.9")
    assert weights["BBB"] == Decimal("0.1")


def test_leveraged_sleeve_survives_the_merge(tmp_path):
    # A sleeve whose weights sum past 1.0 stays leveraged in the merged book —
    # msts-trader allows gross > 100% (leverage comes from the weights).
    lev = _write(tmp_path, "lev.csv", "ticker,weight\nSPY,1.5\n")

    weights, _ = merge_sleeves.merge([(Decimal("50000"), lev)])

    assert weights["SPY"] == Decimal("1.5")


def test_comments_bom_and_odd_headers_are_tolerated(tmp_path):
    # Same tolerances as msts-trader's own parser, so a signed/stamped weights
    # feed can be merged without pre-cleaning.
    p = tmp_path / "feed.csv"
    p.write_text("# asof: 2026-08-29T14:00:00Z\n Ticker , Weight \nspy,0.75\n\nGLD,0.25\n", encoding="utf-8-sig")

    weights, _ = merge_sleeves.merge([(Decimal("10000"), str(p))])

    assert weights == {"SPY": Decimal("0.75"), "GLD": Decimal("0.25")}


# -------------------------------------------------------------- exit rows ---


def test_netted_to_zero_ticker_is_emitted_as_an_explicit_zero_row(tmp_path, monkeypatch, capsys):
    """The --no-sweep trap: an UNLISTED ticker is left alone, so a dropped row
    would strand the position forever. Weight 0 means "sell it all" in both
    sweep modes, so the row is always emitted."""
    sleeve = _write(tmp_path, "s.csv", "ticker,weight\nSPY,1.0\nEEM,0\n")

    rows = _rows(_run(monkeypatch, capsys, "50000", sleeve))

    assert rows["EEM"] == "0.000000"
    assert rows["SPY"] == "1.000000"


def test_ticker_zeroed_by_one_sleeve_still_nets_out_against_another(tmp_path, monkeypatch, capsys):
    # One sleeve exiting a name the other still wants must NOT close it.
    a = _write(tmp_path, "a.csv", "ticker,weight\nSPY,0\n")
    b = _write(tmp_path, "b.csv", "ticker,weight\nSPY,1.0\n")

    rows = _rows(_run(monkeypatch, capsys, "50000", a, "50000", b))

    assert rows["SPY"] == "0.500000"


# ------------------------------------------------------------------ stops ---


@pytest.mark.parametrize("order", [("0.05", "0.015"), ("0.015", "0.05")])
def test_tightest_stop_wins_on_a_shared_ticker(tmp_path, order):
    # A ticker carries only one stop_pct; disagreeing sleeves resolve to the
    # tighter one rather than silently taking whichever was read last — so the
    # result must not depend on the order the sleeves are passed in.
    first, second = order
    a = _write(tmp_path, "a.csv", f"ticker,weight,stop_pct\nSPY,1.0,{first}\n")
    b = _write(tmp_path, "b.csv", f"ticker,weight,stop_pct\nSPY,1.0,{second}\n")

    _, stops = merge_sleeves.merge([(Decimal("50000"), a), (Decimal("50000"), b)])

    assert stops["SPY"] == Decimal("0.015")


def test_no_stop_column_when_no_sleeve_sets_one(tmp_path, monkeypatch, capsys):
    sleeve = _write(tmp_path, "s.csv", "ticker,weight\nSPY,1.0\n")

    out = _run(monkeypatch, capsys, "50000", sleeve)

    assert out.splitlines()[0] == "ticker,weight"


def test_exit_row_carries_no_stop(tmp_path, monkeypatch, capsys):
    # A stop on a weight-0 row would be a stop for shares we're selling.
    sleeve = _write(tmp_path, "s.csv", "ticker,weight,stop_pct\nSPY,1.0,0.02\nEEM,0,0.03\n")

    out = _run(monkeypatch, capsys, "50000", sleeve)
    body = {ln.split(",")[0]: ln.split(",")[2] for ln in out.strip().splitlines()[1:]}

    assert body["EEM"] == ""
    assert body["SPY"] == "0.02"


# ------------------------------------------------------------------- argv ---


@pytest.mark.parametrize("argv", [[], ["50000"], ["50000", "a.csv", "30000"]])
def test_odd_or_empty_argv_is_refused(argv, monkeypatch, capsys):
    with pytest.raises(SystemExit) as e:
        _run(monkeypatch, capsys, *argv)
    assert e.value.code != 0


def test_non_numeric_allocation_is_refused(tmp_path, monkeypatch, capsys):
    sleeve = _write(tmp_path, "s.csv", "ticker,weight\nSPY,1.0\n")
    with pytest.raises(SystemExit):
        _run(monkeypatch, capsys, "fifty-thousand", sleeve)


def test_zero_total_allocation_is_refused(tmp_path):
    sleeve = _write(tmp_path, "s.csv", "ticker,weight\nSPY,1.0\n")
    with pytest.raises(SystemExit):
        merge_sleeves.merge([(Decimal("0"), sleeve)])


def test_non_numeric_weight_names_the_file_and_ticker(tmp_path):
    sleeve = _write(tmp_path, "s.csv", "ticker,weight\nSPY,lots\n")
    with pytest.raises(SystemExit) as e:
        merge_sleeves.merge([(Decimal("50000"), sleeve)])
    assert "SPY" in str(e.value.code)


# ------------------------------------------------------- README round-trip ---


def test_merged_output_parses_and_drives_the_diff_engine(tmp_path, monkeypatch, capsys):
    """The README's promise, end to end: an $80k algo book fenced inside a
    $200k account that the owner also trades by hand.

    Manual positions must be untouched, the zeroed name must close, and every
    algo line must size against the allocation — never against account NAV.
    """
    momo = _write(tmp_path, "momo.csv", "ticker,weight\nSPY,0.60\nGLD,0.40\nEEM,0\n")
    carry = _write(tmp_path, "carry.csv", "ticker,weight\nSPY,0.50\nSHV,0.50\n")

    targets = parse_csv(_run(monkeypatch, capsys, "50000", momo, "30000", carry))

    positions = {
        "AAPL": Position("AAPL", Decimal("400"), Decimal("200")),  # $80k, traded by hand
        "TSLA": Position("TSLA", Decimal("100"), Decimal("300")),  # $30k, traded by hand
        "EEM": Position("EEM", Decimal("200"), Decimal("50")),  # $10k algo leftover
        "SPY": Position("SPY", Decimal("20"), Decimal("500")),  # $10k algo
    }
    quotes = {
        "AAPL": Decimal("200"),
        "TSLA": Decimal("300"),
        "EEM": Decimal("50"),
        "SPY": Decimal("500"),
        "GLD": Decimal("250"),
        "SHV": Decimal("110"),
    }

    p = build_preview(
        targets=targets,
        positions=positions,
        nav=Decimal("200000"),
        cash=Decimal("50000"),
        buying_power=Decimal("300000"),
        quotes=quotes,
        allocation=Decimal("80000"),
        sweep=False,
        drift_mode="position",
    )

    assert p.blockers == []
    traded = {o.ticker for o in p.orders}
    assert "AAPL" not in traded and "TSLA" not in traded
    for tkr in ("AAPL", "TSLA"):
        row = next(r for r in p.rows if r.ticker == tkr)
        assert row.order is None and "kept" in row.note

    eem = next(o for o in p.orders if o.ticker == "EEM")
    assert eem.side == Side.SELL and eem.quantity == Decimal("200")

    # Sized against the $80k allocation, not the $200k NAV.
    spy = next(o for o in p.orders if o.ticker == "SPY")
    assert spy.side == Side.BUY and spy.notional == Decimal("35000")  # .5625*80k - 10k held
    gld = next(o for o in p.orders if o.ticker == "GLD")
    assert gld.notional == Decimal("20000")  # .25 * 80k
    assert any("80,000 allocation" in w for w in p.warnings)

    # Sells are ordered before buys so the cash is there to spend.
    sides = [o.side for o in p.orders]
    assert sides.index(Side.SELL) < sides.index(Side.BUY)
