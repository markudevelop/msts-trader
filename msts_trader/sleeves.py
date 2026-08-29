"""Per-strategy share tallies ("sleeves") inside one brokerage account.

msts-trader normally reads positions at the ACCOUNT level, so two strategies —
or a strategy and the owner's manual trades — sharing a ticker trade against
each other. A sleeve fixes that with a local ledger: every order the tool sends
under ``--sleeve NAME`` is recorded, and the confirmed fills accumulate into a
per-(sleeve, ticker) share tally. Sizing then reads the sleeve's tally instead
of the account position.

The invariant is deliberately an INEQUALITY:

    for every ticker:   sum(sleeve tallies)  <=  account position

The gap is UNASSIGNED — shares the tool never bought. The owner's manual book
is unassigned by construction: they declare nothing, and the tool structurally
cannot touch shares it didn't buy. Equality must never be asserted, because
shares move with no tagged order behind them (manual fills, splits, spin-offs,
DRIP, transfers).

Design notes (see docs/design-strategy-sleeves.md):
  - Tallies are written from ``order_status()``'s ``filled_qty`` — never from
    the ordered quantity, which corrupts the tally on the first partial fill.
  - Orders not terminal after the post-send poll go into ``pending`` with a
    ``recorded_fill`` cursor; every later run settles them idempotently
    (only ``filled - recorded`` is applied, then the cursor advances).
  - A NEGATIVE residual (tallies claim more than the account holds) means algo
    shares vanished outside the tool. Nobody can know whose shares they were,
    so the caller must refuse to trade and send the operator to
    ``msts-trader sleeve reconcile`` — never auto-heal.
  - The ledger is money-adjacent local state: Decimal-as-string on disk,
    atomic replace with a ``.bak`` of the previous version, and an O_EXCL
    lockfile (same pattern as runstate) around read-modify-write cycles.
"""

from __future__ import annotations

import json
import os
import re
import time as _time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .models import Position

LEDGER_DIR = Path(os.path.expanduser("~/.msts-trader/sleeves"))

# Terminal order states: nothing more can fill, so the pending entry is done.
# (Statuses are the normalized lowercase strings from brokers/base.py; UNKNOWN
# is NOT terminal — a flaky status read must not orphan a live order.)
_TERMINAL = {"filled", "cancelled", "canceled", "rejected", "expired"}


class SleeveError(ValueError):
    pass


@dataclass
class Ledger:
    broker: str
    account: str
    # sleeve name -> ticker -> shares the sleeve owns (confirmed fills only)
    sleeves: dict[str, dict[str, Decimal]] = field(default_factory=dict)
    # in-flight orders awaiting fills: dicts with order_id/sleeve/ticker/side/
    # requested/recorded_fill/ts
    pending: list[dict] = field(default_factory=list)

    def tally(self, sleeve: str, ticker: str) -> Decimal:
        return self.sleeves.get(sleeve, {}).get(ticker.upper(), Decimal(0))

    def claims(self) -> dict[str, Decimal]:
        """Total shares claimed per ticker across ALL sleeves."""
        out: dict[str, Decimal] = {}
        for book in self.sleeves.values():
            for tkr, qty in book.items():
                out[tkr] = out.get(tkr, Decimal(0)) + qty
        return out

    def has_entries(self) -> bool:
        return any(any(q > 0 for q in book.values()) for book in self.sleeves.values()) or bool(self.pending)


def _path(broker: str, account: str) -> Path:
    # Account ids can carry filesystem-hostile chars (Schwab hashes); keep it flat.
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", f"{broker}_{account}")
    return LEDGER_DIR / f"{safe}.json"


def load(broker: str, account: str) -> Ledger:
    """Load the (broker, account) ledger; a missing file is an empty ledger."""
    p = _path(broker, account)
    if not p.exists():
        return Ledger(broker=broker, account=account)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise SleeveError(f"sleeve ledger {p} is unreadable ({e}) — restore {p}.bak or delete it") from e
    try:
        sleeves = {
            name: {tkr.upper(): Decimal(q) for tkr, q in book.items()}
            for name, book in (data.get("sleeves") or {}).items()
        }
    except (InvalidOperation, AttributeError) as e:
        raise SleeveError(f"sleeve ledger {p} has a corrupt quantity ({e}) — restore {p}.bak") from e
    return Ledger(broker=broker, account=account, sleeves=sleeves, pending=list(data.get("pending") or []))


def save(ledger: Ledger) -> Path:
    """Atomic write with a .bak of the previous version — a torn write must
    never be able to destroy the tally."""
    p = _path(ledger.broker, ledger.account)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "broker": ledger.broker,
        "account": ledger.account,
        "sleeves": {
            name: {tkr: str(qty) for tkr, qty in sorted(book.items()) if qty > 0}
            for name, book in sorted(ledger.sleeves.items())
        },
        "pending": ledger.pending,
    }
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if p.exists():
        bak = p.with_suffix(".json.bak")
        try:
            os.replace(p, bak)
        except OSError:
            pass
    os.replace(tmp, p)
    return p


class lock:
    """O_EXCL lockfile with a bounded wait (same pattern as runstate.record).

    Serializes two sleeve runs against the same account; best-effort if the
    lock cannot be acquired (the run proceeds — the residual gate still fails
    closed on any inconsistency it would have caused).
    """

    def __init__(self, broker: str, account: str, timeout_s: float = 10.0):
        self._lock = _path(broker, account).with_suffix(".json.lock")
        self._timeout = timeout_s
        self._have = False

    def __enter__(self):
        self._lock.parent.mkdir(parents=True, exist_ok=True)
        deadline = _time.monotonic() + self._timeout
        while _time.monotonic() < deadline:
            try:
                fd = os.open(str(self._lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                self._have = True
                return self
            except FileExistsError:
                _time.sleep(0.1)
        return self  # best-effort

    def __exit__(self, *exc):
        if self._have:
            try:
                self._lock.unlink()
            except OSError:
                pass
        return False


def apply_fill(ledger: Ledger, sleeve: str, ticker: str, side: str, qty: Decimal) -> None:
    """Apply a CONFIRMED fill to a sleeve's tally. Sells clamp at zero — a
    broker over-reporting fills must never drive a tally negative."""
    if qty <= 0:
        return
    tkr = ticker.upper()
    book = ledger.sleeves.setdefault(sleeve, {})
    cur = book.get(tkr, Decimal(0))
    if side.upper() == "BUY":
        book[tkr] = cur + qty
    else:
        book[tkr] = max(Decimal(0), cur - qty)


def record_order(ledger: Ledger, sleeve: str, *, order_id: str, ticker: str, side: str, requested: Decimal) -> None:
    """Register a just-placed order as pending. Fills are applied only by
    settle_pending — never here (intent is not a fill)."""
    ledger.pending.append(
        {
            "order_id": str(order_id),
            "sleeve": sleeve,
            "ticker": ticker.upper(),
            "side": side.upper(),
            "requested": str(requested),
            "recorded_fill": "0",
        }
    )


def settle_pending(ledger: Ledger, broker) -> list[str]:
    """Poll order_status for every pending order and apply NEW fills.

    Idempotent via the recorded_fill cursor: only ``filled - recorded`` is
    applied, so a re-poll (or a run that crashed after applying) never
    double-counts. Terminal orders are dropped; anything else (resting, MOC
    awaiting the close, a flaky UNKNOWN read) stays pending for the next run.
    Returns human-readable notes for anything that changed.
    """
    notes: list[str] = []
    still: list[dict] = []
    for entry in ledger.pending:
        try:
            st = broker.order_status(entry["order_id"])
        except Exception:
            still.append(entry)  # can't read -> keep, retry next run
            continue
        status = str(st.get("status", "")).lower()
        requested = Decimal(entry["requested"])
        recorded = Decimal(entry["recorded_fill"])
        # Cap at the requested quantity: an over-reporting broker must not
        # inflate the tally past what the order asked for.
        filled = min(Decimal(str(st.get("filled_qty") or 0)), requested)
        delta = filled - recorded
        if delta > 0:
            apply_fill(ledger, entry["sleeve"], entry["ticker"], entry["side"], delta)
            entry["recorded_fill"] = str(filled)
            notes.append(f"{entry['sleeve']}: {entry['ticker']} {entry['side']} settled {delta} (order {status})")
        if status not in _TERMINAL:
            still.append(entry)
    ledger.pending = still
    return notes


def negative_residuals(ledger: Ledger, positions: dict[str, Position]) -> dict[str, Decimal]:
    """Tickers where the sleeves claim MORE shares than the account holds.

    That means algo shares vanished outside the tool (manual sell, corporate
    action, an unattributed stop fill). The caller must refuse to trade on any
    of these — auto-healing is a guess that mis-sizes a real order.
    """
    out: dict[str, Decimal] = {}
    for tkr, claimed in ledger.claims().items():
        held = positions.get(tkr).quantity if tkr in positions else Decimal(0)
        if claimed > held:
            out[tkr] = held - claimed  # negative
    return out


def sleeve_view(ledger: Ledger, sleeve: str, positions: dict[str, Position]) -> dict[str, Position]:
    """The account as this sleeve sees it: only its own tally, clamped to what
    the account actually holds. Unassigned shares (the owner's manual book) and
    other sleeves' shares are invisible — the diff engine can neither sell nor
    sweep them."""
    view: dict[str, Position] = {}
    for tkr, tally in ledger.sleeves.get(sleeve, {}).items():
        acct = positions.get(tkr)
        if acct is None:
            continue
        eff = min(tally, acct.quantity)
        if eff > 0:
            view[tkr] = Position(ticker=tkr, quantity=eff, price=acct.price)
    return view


def adopt(ledger: Ledger, sleeve: str, ticker: str, qty: Decimal, positions: dict[str, Position]) -> None:
    """Assign already-held shares to a sleeve (day-one bootstrap). Refuses to
    push total claims past the account position — adopting shares that don't
    exist would fabricate a tally the residual gate then trips on."""
    tkr = ticker.upper()
    if qty <= 0:
        raise SleeveError("adopt quantity must be positive")
    held = positions.get(tkr).quantity if tkr in positions else Decimal(0)
    claimed = ledger.claims().get(tkr, Decimal(0))
    if claimed + qty > held:
        raise SleeveError(
            f"cannot adopt {qty} {tkr}: account holds {held}, sleeves already claim {claimed} "
            f"(only {max(held - claimed, Decimal(0))} unassigned)"
        )
    book = ledger.sleeves.setdefault(sleeve, {})
    book[tkr] = book.get(tkr, Decimal(0)) + qty


def release(ledger: Ledger, sleeve: str, ticker: str, qty: Decimal) -> None:
    """Return shares from a sleeve's tally to unassigned (the tool stops
    managing them; nothing is traded)."""
    tkr = ticker.upper()
    cur = ledger.tally(sleeve, tkr)
    if qty <= 0 or qty > cur:
        raise SleeveError(f"cannot release {qty} {tkr} from {sleeve}: tally is {cur}")
    ledger.sleeves[sleeve][tkr] = cur - qty


def adjust(ledger: Ledger, sleeve: str, ticker: str, qty: Decimal) -> None:
    """Operator override during reconciliation: assert the true tally. No
    bounds check — this IS the manual fix — but the residual gate still
    refuses the next run if the assertion exceeds the account position."""
    if qty < 0:
        raise SleeveError("tally cannot be negative")
    ledger.sleeves.setdefault(sleeve, {})[ticker.upper()] = qty
