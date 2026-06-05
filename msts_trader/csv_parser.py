from __future__ import annotations

import csv
import io
from decimal import Decimal, InvalidOperation

from .models import Target


class CSVParseError(ValueError):
    pass


REQUIRED_HEADERS = {"ticker", "weight"}


def parse_csv(text: str) -> list[Target]:
    """Parse a `ticker,weight` CSV. Tolerates BOM, blank lines, comments, surrounding whitespace.

    Comment lines start with `#` (e.g. trailing `# sig: ed25519:...`) and are ignored.
    """
    text = text.lstrip("﻿").strip()
    if not text:
        raise CSVParseError("empty input")

    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        raise CSVParseError("no data rows")

    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    headers = {h.strip().lower() for h in (reader.fieldnames or [])}
    missing = REQUIRED_HEADERS - headers
    if missing:
        raise CSVParseError(f"missing required columns: {sorted(missing)} — got {sorted(headers)}")

    targets: list[Target] = []
    seen: set[str] = set()
    for i, row in enumerate(reader, start=2):
        tkr = (row.get("ticker") or row.get("Ticker") or "").strip().upper()
        raw_w = (row.get("weight") or row.get("Weight") or "").strip()
        if not tkr:
            continue
        if tkr in seen:
            raise CSVParseError(f"line {i}: duplicate ticker {tkr}")
        try:
            w = Decimal(raw_w)
        except InvalidOperation:
            raise CSVParseError(f"line {i}: weight {raw_w!r} is not a number")
        if w < 0:
            raise CSVParseError(f"line {i}: negative weight {w} for {tkr} (shorts unsupported in v1)")
        if w > 1:
            raise CSVParseError(f"line {i}: weight {w} > 1 for {tkr} (expected fraction, not percent)")
        seen.add(tkr)
        targets.append(Target(ticker=tkr, weight=w))

    if not targets:
        raise CSVParseError("no targets parsed (all rows blank?)")
    return targets


def total_weight(targets: list[Target]) -> Decimal:
    return sum((t.weight for t in targets), Decimal(0))
