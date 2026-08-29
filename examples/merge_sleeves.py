#!/usr/bin/env python3
"""Merge several strategy "sleeves" into ONE target-weight CSV for msts-trader.

msts-trader has no per-strategy position ledger: it reads the *account's*
position in a ticker and sizes against that. So two strategies run as two
separate rebalances will fight over any ticker they share — each sees the
other's shares as its own drift and trades them away.

The fix needs no ledger at all: sum the sleeves yourself and send ONE combined
book. Each sleeve keeps its own weights CSV (weights are fractions of that
sleeve) plus a dollar allocation; this script converts every line into a
fraction of TOTAL NAV and adds up any ticker more than one sleeve holds. A
single rebalance then lands the account on exactly the combined target.

    python merge_sleeves.py 50000 momo.csv 30000 carry.csv > combined.csv
    msts-trader rebalance --csv-file combined.csv --dry-run

Notes:
  - Keep the default --sweep: the merged CSV *is* the complete book.
  - Drift is measured on the combined book, so a small sleeve may never breach
    4% of total NAV. Add --threshold-mode position (or a lower --threshold) if
    you want small sleeves to trade.
  - A ticker can carry only one stop_pct. When sleeves disagree on a shared
    name the tightest stop wins — override by hand if you want the other one.
  - Attribution stays here, in dollars: sleeve i owns
    (its weight * its allocation) of each ticker.
"""

from __future__ import annotations

import csv
import sys
from collections import defaultdict
from decimal import Decimal, ROUND_DOWN

USAGE = "usage: merge_sleeves.py <alloc$> <sleeve.csv> [<alloc$> <sleeve.csv> ...]"


def read_sleeve(path: str) -> list[dict[str, str]]:
    """Rows of a sleeve CSV, `#` comment lines dropped and headers normalised.

    Same tolerances as msts-trader's own parser: BOM, blank lines, comment
    lines (`# asof: ...`), and stray case/whitespace in the headers.
    """
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = csv.DictReader(ln for ln in f if ln.strip() and not ln.lstrip().startswith("#"))
        return [{(k or "").strip().lower(): (v or "").strip() for k, v in row.items()} for row in rows]


def merge(sleeves: list[tuple[Decimal, str]]) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    """(weights of total NAV, stop_pct) keyed by ticker."""
    nav = sum(alloc for alloc, _ in sleeves)
    if nav <= 0:
        sys.exit("total allocation must be positive")

    weights: dict[str, Decimal] = defaultdict(Decimal)
    stops: dict[str, Decimal] = {}
    for alloc, path in sleeves:
        for row in read_sleeve(path):
            ticker = row.get("ticker", "").upper()
            if not ticker:
                continue
            try:
                weight = Decimal(row["weight"])
            except (KeyError, ArithmeticError):
                sys.exit(f"{path}: {ticker} has a missing or non-numeric weight")
            # Sleeve fraction -> fraction of the whole account. A ticker held by
            # two sleeves accumulates: msts-trader rejects duplicate rows, and
            # the account can only hold one combined position anyway.
            weights[ticker] += weight * alloc / nav
            if row.get("stop_pct"):
                stop = Decimal(row["stop_pct"])
                stops[ticker] = stop if ticker not in stops else min(stops[ticker], stop)
    return weights, stops


def main(argv: list[str]) -> None:
    if len(argv) < 2 or len(argv) % 2:
        sys.exit(USAGE)
    try:
        sleeves = [(Decimal(argv[i]), argv[i + 1]) for i in range(0, len(argv), 2)]
    except ArithmeticError:
        sys.exit(USAGE)

    weights, stops = merge(sleeves)
    out = csv.writer(sys.stdout, lineterminator="\n")
    out.writerow(["ticker", "weight"] + (["stop_pct"] if stops else []))
    for ticker in sorted(weights):
        # Round DOWN so the merged book never sizes above the sleeves' intent.
        weight = weights[ticker].quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        if weight <= 0:
            continue  # nobody wants it — the default sweep closes any held position
        out.writerow([ticker, weight] + ([stops.get(ticker, "")] if stops else []))


if __name__ == "__main__":
    main(sys.argv[1:])
