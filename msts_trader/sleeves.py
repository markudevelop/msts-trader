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
    # sleeve name -> virtual cash the sleeve manages. Present = cash-tracked:
    # the sleeve's sizing base is its OWN NAV (cash + holdings), it compounds
    # its gains and absorbs its losses, and confirmed fills move this cash.
    # Absent (legacy v1 sleeves) = the sleeve sizes against --allocation.
    # Purely bookkeeping — invest/divest never moves real money; the dollars
    # all live in the one brokerage account (that's the cross-margin point).
    cash: dict[str, Decimal] = field(default_factory=dict)
    # sleeve name -> sizing policy: {"base": {"mode": ..., "value": ...},
    # "cap": {"mode": ..., "value": ...} | None}. Modes for base:
    #   "own-nav" (default): the sleeve's cash + holdings — compounds.
    #   "pct-nav": a fraction of ACCOUNT NAV — the sleeve's capital floats
    #       with the whole account (explicit opt back into rebalancing
    #       capital between the sleeve and everything else).
    #   "fixed": a static dollar figure (the old --allocation semantics,
    #       explicit and persistent instead of a per-run flag).
    # "cap" bounds the computed base from above: dollars or a fraction of
    # account NAV. Values stored as strings, parsed to Decimal.
    policy: dict[str, dict] = field(default_factory=dict)
    # sleeve name -> cumulative NET capital contributed (invests - divests,
    # plus adopted-in / released-out share value at the mark of the moment).
    # P&L = sleeve NAV - contributed. Absent for sleeves created before this
    # field existed — their P&L reads n/a until capital is re-seeded.
    contributed: dict[str, Decimal] = field(default_factory=dict)
    # in-flight orders awaiting fills: dicts with order_id/sleeve/ticker/side/
    # requested/recorded_fill/recorded_cost/est_price/ts
    pending: list[dict] = field(default_factory=list)

    def tally(self, sleeve: str, ticker: str) -> Decimal:
        return self.sleeves.get(sleeve, {}).get(ticker.upper(), Decimal(0))

    def cash_tracked(self, sleeve: str) -> bool:
        return sleeve in self.cash

    def configured(self, sleeve: str) -> bool:
        """True when the sleeve has its own sizing (cash and/or a policy) —
        which makes a per-run --allocation both redundant and dangerous."""
        return sleeve in self.cash or sleeve in self.policy

    def claims(self) -> dict[str, Decimal]:
        """Total shares claimed per ticker across ALL sleeves."""
        out: dict[str, Decimal] = {}
        for book in self.sleeves.values():
            for tkr, qty in book.items():
                out[tkr] = out.get(tkr, Decimal(0)) + qty
        return out

    def has_entries(self) -> bool:
        return (
            any(any(q > 0 for q in book.values()) for book in self.sleeves.values())
            or bool(self.pending)
            or bool(self.cash)
        )


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
    try:
        cash = {name: Decimal(v) for name, v in (data.get("cash") or {}).items()}
    except InvalidOperation as e:
        raise SleeveError(f"sleeve ledger {p} has a corrupt cash value ({e}) — restore {p}.bak") from e
    return Ledger(
        broker=broker,
        account=account,
        sleeves=sleeves,
        cash=cash,
        policy=dict(data.get("policy") or {}),
        contributed={name: Decimal(v) for name, v in (data.get("contributed") or {}).items()},
        pending=list(data.get("pending") or []),
    )


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
        "cash": {name: str(v) for name, v in sorted(ledger.cash.items())},
        "policy": {name: v for name, v in sorted(ledger.policy.items())},
        "contributed": {name: str(v) for name, v in sorted(ledger.contributed.items())},
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


def record_order(
    ledger: Ledger,
    sleeve: str,
    *,
    order_id: str,
    ticker: str,
    side: str,
    requested: Decimal,
    est_price: Decimal | None = None,
) -> None:
    """Register a just-placed order as pending. Fills are applied only by
    settle_pending — never here (intent is not a fill). est_price is the
    cash-accounting fallback for brokers whose order_status carries no
    filled_avg_price."""
    ledger.pending.append(
        {
            "order_id": str(order_id),
            "sleeve": sleeve,
            "ticker": ticker.upper(),
            "side": side.upper(),
            "requested": str(requested),
            "recorded_fill": "0",
            "recorded_cost": "0",
            "est_price": str(est_price) if est_price else None,
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
            # Cash-tracked sleeve: move the fill's dollars between cash and
            # holdings. Cost is cursor-based like the fill itself — the
            # broker's filled_avg_price is a CUMULATIVE average, so
            # (filled * avg) - recorded_cost is exact across partial fills at
            # different prices; est_price is the fallback for adapters whose
            # order_status has no average (e.g. hyperliquid).
            if ledger.cash_tracked(entry["sleeve"]):
                avg = st.get("filled_avg_price")
                px = Decimal(str(avg)) if avg else (Decimal(entry["est_price"]) if entry.get("est_price") else None)
                if px is not None and px > 0:
                    total_cost = filled * px
                    delta_cost = total_cost - Decimal(entry.get("recorded_cost", "0"))
                    entry["recorded_cost"] = str(total_cost)
                    sl = entry["sleeve"]
                    if entry["side"] == "BUY":
                        ledger.cash[sl] = ledger.cash[sl] - delta_cost
                    else:
                        ledger.cash[sl] = ledger.cash[sl] + delta_cost
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
    # Adopted shares are contributed capital, marked at the position's price
    # right now — so they don't show up as instant P&L.
    px = positions[tkr].price if tkr in positions else Decimal(0)
    if px > 0:
        ledger.contributed[sleeve] = ledger.contributed.get(sleeve, Decimal(0)) + qty * px


def release(
    ledger: Ledger, sleeve: str, ticker: str, qty: Decimal, positions: dict[str, Position] | None = None
) -> None:
    """Return shares from a sleeve's tally to unassigned (the tool stops
    managing them; nothing is traded). When positions are supplied, the
    released value is deducted from the contribution record at today's mark —
    the mirror of adopt — so P&L doesn't read the departure as a loss."""
    tkr = ticker.upper()
    cur = ledger.tally(sleeve, tkr)
    if qty <= 0 or qty > cur:
        raise SleeveError(f"cannot release {qty} {tkr} from {sleeve}: tally is {cur}")
    ledger.sleeves[sleeve][tkr] = cur - qty
    if positions and tkr in positions and sleeve in ledger.contributed:
        px = positions[tkr].price
        if px > 0:
            ledger.contributed[sleeve] -= qty * px


def adjust(ledger: Ledger, sleeve: str, ticker: str, qty: Decimal) -> None:
    """Operator override during reconciliation: assert the true tally. No
    bounds check — this IS the manual fix — but the residual gate still
    refuses the next run if the assertion exceeds the account position."""
    if qty < 0:
        raise SleeveError("tally cannot be negative")
    ledger.sleeves.setdefault(sleeve, {})[ticker.upper()] = qty


def invest(ledger: Ledger, sleeve: str, amount: Decimal, baseline: Decimal | None = None) -> Decimal:
    """Add virtual capital to a sleeve (and switch it to cash-tracked sizing).

    Bookkeeping only — no money moves; the dollars already live in the one
    brokerage account. From the first invest on, the sleeve's sizing base is
    its OWN NAV (cash + holdings): gains compound inside the sleeve and losses
    are its own to dig out of — the account's other money is never pulled in
    unless the operator invests more. Returns the new cash balance."""
    if amount <= 0:
        raise SleeveError("invest amount must be positive")
    ledger.cash[sleeve] = ledger.cash.get(sleeve, Decimal(0)) + amount
    # Contribution record (P&L baseline). `baseline` seeds the pre-existing
    # value of a sleeve created before contributions were tracked, so its P&L
    # starts at ~0 from this moment instead of counting old value as profit.
    prior = ledger.contributed.get(sleeve)
    if prior is None and baseline is not None:
        prior = baseline
    ledger.contributed[sleeve] = (prior or Decimal(0)) + amount
    return ledger.cash[sleeve]


def divest(ledger: Ledger, sleeve: str, amount: Decimal, nav: Decimal | None = None) -> Decimal:
    """Withdraw virtual capital from a sleeve — the exact mirror of invest.

    The amount comes straight off the sleeve's cash, and MAY drive it
    negative: the next rebalance sizes against the reduced NAV and sells
    holdings down to the new target, which brings the cash back toward zero
    on its own (with weights summing to 1.0, it sells almost exactly the
    divested amount). No weight-fiddling required.

    Bounded by the sleeve's NAV when supplied — you cannot take out more
    than the sleeve is worth. Returns the new cash balance."""
    cur = ledger.cash.get(sleeve)
    if cur is None:
        raise SleeveError(f"sleeve {sleeve!r} does not track cash — nothing to divest")
    if amount <= 0:
        raise SleeveError("divest amount must be positive")
    if nav is not None and amount > nav:
        raise SleeveError(
            f"cannot divest {amount}: sleeve {sleeve!r} is only worth ${nav:,.2f} "
            f"(cash + holdings) — that is the most it can give back"
        )
    ledger.cash[sleeve] = cur - amount
    if sleeve in ledger.contributed:  # legacy sleeves have no record to reduce
        ledger.contributed[sleeve] -= amount
    return ledger.cash[sleeve]


def sleeve_nav(ledger: Ledger, sleeve: str, positions: dict[str, Position], quotes: dict[str, Decimal]) -> Decimal:
    """The sleeve's own NAV: its cash plus its holdings at live quotes (the
    account-position price is the fallback, mirroring diff's `_mv`). This is
    the sizing base for a cash-tracked sleeve — the number that lets it
    compound gains and forces it to absorb losses."""
    nav = ledger.cash.get(sleeve, Decimal(0))
    for tkr, pos in sleeve_view(ledger, sleeve, positions).items():
        px = quotes.get(tkr)
        if px is None or px <= 0:
            px = pos.price
        nav += pos.quantity * px
    return nav


def parse_amount(text: str) -> tuple[str, Decimal]:
    """Parse a CLI capital figure: '20%' -> ("pct-nav", 0.20) — a fraction of
    account NAV — while '$50000' / '50000' -> ("fixed", 50000) dollars."""
    t = text.strip().replace(",", "").lstrip("$")
    try:
        if t.endswith("%"):
            pct = Decimal(t[:-1]) / 100
            if not (Decimal(0) < pct <= Decimal(1)):
                raise SleeveError(f"{text!r}: percent must be in (0, 100]")
            return "pct-nav", pct
        val = Decimal(t)
        if val <= 0:
            raise SleeveError(f"{text!r}: amount must be positive")
        return "fixed", val
    except InvalidOperation as e:
        raise SleeveError(f"{text!r} is not an amount — use e.g. 50000, $50000 or 20%") from e


def set_base(ledger: Ledger, sleeve: str, spec: str) -> str:
    """Set the sleeve's sizing base: 'own-nav' (compounding, the default),
    'X%' of account NAV, or a fixed dollar figure. Returns a description."""
    entry = ledger.policy.setdefault(sleeve, {})
    if spec.strip().lower() in ("own-nav", "own", "nav"):
        entry.pop("base", None)  # own-nav IS the default — store nothing
        if not entry:
            ledger.policy.pop(sleeve, None)
        return "own NAV (cash + holdings, compounding)"
    mode, val = parse_amount(spec)
    entry["base"] = {"mode": mode, "value": str(val)}
    return f"{val * 100}% of account NAV" if mode == "pct-nav" else f"fixed ${val:,.2f}"


def set_cap(ledger: Ledger, sleeve: str, spec: str) -> str:
    """Cap the sleeve's sizing base from above ($X or X% of account NAV);
    'off' removes the cap. Returns a description."""
    entry = ledger.policy.setdefault(sleeve, {})
    if spec.strip().lower() in ("off", "none"):
        entry.pop("cap", None)
        if not entry:
            ledger.policy.pop(sleeve, None)
        return "no cap"
    mode, val = parse_amount(spec)
    entry["cap"] = {"mode": mode, "value": str(val)}
    return f"cap {val * 100}% of account NAV" if mode == "pct-nav" else f"cap ${val:,.2f}"


def sizing_base(
    ledger: Ledger,
    sleeve: str,
    *,
    account_nav: Decimal,
    positions: dict[str, Position],
    quotes: dict[str, Decimal],
) -> tuple[Decimal, str] | None:
    """The dollars this sleeve's weights apply to, per its policy, cap applied.

    None = the sleeve has neither cash nor a policy (a legacy 0.28.0 sleeve) —
    the caller falls back to --allocation. Cash bookkeeping is orthogonal: an
    invested sleeve on a pct-nav or fixed base still settles its fills through
    its cash (P&L attribution), the base just stops depending on it.
    """
    if not ledger.configured(sleeve):
        return None
    entry = ledger.policy.get(sleeve, {})
    base_spec = entry.get("base")
    if base_spec is None:
        base = sleeve_nav(ledger, sleeve, positions, quotes)
        desc = f"own NAV ${base:,.2f}"
    elif base_spec["mode"] == "pct-nav":
        pct = Decimal(base_spec["value"])
        base = account_nav * pct
        desc = f"{pct * 100}% of account NAV = ${base:,.2f}"
    else:
        base = Decimal(base_spec["value"])
        desc = f"fixed ${base:,.2f}"
    cap_spec = entry.get("cap")
    if cap_spec is not None:
        cap = account_nav * Decimal(cap_spec["value"]) if cap_spec["mode"] == "pct-nav" else Decimal(cap_spec["value"])
        if base > cap:
            base = cap
            desc += f", capped at ${cap:,.2f}"
    return base, desc


def cash_overclaim(ledger: Ledger, account_cash: Decimal) -> Decimal:
    """How far the sleeves' combined virtual cash exceeds the account's REAL
    cash (0 when fully backed). Not an error — in a margin account the excess
    is simply the cross-margin borrow the sleeves would draw on — but worth a
    warning, since a cash account would start rejecting those buys."""
    total = sum(ledger.cash.values(), Decimal(0))
    return max(Decimal(0), total - account_cash)
