from __future__ import annotations

from decimal import Decimal

import pytest

from msts_trader.csv_parser import CSVParseError, parse_csv, total_weight


def test_minimal_parse():
    out = parse_csv("ticker,weight\nSPY,0.5\nSHV,0.5\n")
    assert [(t.ticker, t.weight) for t in out] == [("SPY", Decimal("0.5")), ("SHV", Decimal("0.5"))]


def test_normalizes_case():
    out = parse_csv("ticker,weight\nspy,0.3\nshv,0.3\n")
    assert out[0].ticker == "SPY"
    assert out[1].ticker == "SHV"


def test_strips_bom_and_whitespace():
    out = parse_csv("﻿ticker,weight\n  SPY ,0.4 \n")
    assert out[0].ticker == "SPY"
    assert out[0].weight == Decimal("0.4")


def test_ignores_comment_lines():
    csv = "ticker,weight\n# sig: ed25519:abc\nSPY,0.5\n# trailing comment\nSHV,0.5\n"
    out = parse_csv(csv)
    assert len(out) == 2


def test_ignores_blank_lines():
    out = parse_csv("ticker,weight\n\nSPY,0.5\n\nSHV,0.5\n\n")
    assert len(out) == 2


def test_rejects_missing_header():
    with pytest.raises(CSVParseError, match="missing required columns"):
        parse_csv("symbol,weight\nSPY,0.5\n")


def test_rejects_negative_weight():
    with pytest.raises(CSVParseError, match="negative weight"):
        parse_csv("ticker,weight\nSPY,-0.1\n")


def test_rejects_weight_over_one():
    with pytest.raises(CSVParseError, match="expected fraction"):
        parse_csv("ticker,weight\nSPY,42\n")


def test_rejects_non_numeric_weight():
    with pytest.raises(CSVParseError, match="not a number"):
        parse_csv("ticker,weight\nSPY,banana\n")


def test_rejects_duplicate_ticker():
    with pytest.raises(CSVParseError, match="duplicate ticker"):
        parse_csv("ticker,weight\nSPY,0.3\nSPY,0.4\n")


def test_rejects_empty_input():
    with pytest.raises(CSVParseError, match="empty input"):
        parse_csv("   \n   \n")


def test_rejects_no_data_rows():
    with pytest.raises(CSVParseError, match="no data rows"):
        parse_csv("# only a comment\n")


def test_total_weight():
    out = parse_csv("ticker,weight\nSPY,0.3\nGLD,0.2\nSHV,0.5\n")
    assert total_weight(out) == Decimal("1.0")
