from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Protocol, runtime_checkable

from ..models import Order, Position


class BrokerError(RuntimeError):
    """Anything the broker layer can't fix without user action."""


@dataclass(frozen=True)
class LinkedAccount:
    """One brokerage account reachable under the current login/session.

    `id` is the canonical value used for API calls (hash, account number, …).
    `number` is an optional human-readable account number when it differs from
    `id` (Schwab: plaintext number vs encrypted hash).
    """

    id: str
    number: str | None = None

    @property
    def masked(self) -> str:
        """Last-4 display form of the human number, else a short id prefix."""
        n = self.number or self.id
        if len(n) > 4:
            return "…" + n[-4:]
        return n

    def to_dict(self) -> dict[str, str]:
        out = {"id": self.id, "masked": self.masked}
        if self.number:
            out["number"] = self.number
        return out


def mask_account(value: str) -> str:
    """Last-4 mask for account numbers / ids shown in CLI output."""
    v = (value or "").strip()
    if len(v) > 4:
        return "…" + v[-4:]
    return v


def resolve_linked_account(accounts: list[LinkedAccount], identifier: str) -> LinkedAccount:
    """Resolve a user-supplied account selector against linked accounts.

    Matches (in order):
      1. exact full `id` or full `number`
      2. unique last-4 / suffix match on `number` or `id`

    Raises BrokerError on empty input, zero matches, or ambiguous matches.
    """
    ident = (identifier or "").strip()
    if not ident:
        raise BrokerError("account identifier is empty")
    if not accounts:
        raise BrokerError("no linked accounts on this session")

    exact = [a for a in accounts if a.id == ident or (a.number is not None and a.number == ident)]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        opts = ", ".join(a.masked for a in exact)
        raise BrokerError(f"ambiguous account {ident!r} (exact matches: {opts})")

    # Suffix / last-4: only when the identifier is short enough to be a
    # partial selector (full numbers already handled above).
    suffix = [a for a in accounts if a.id.endswith(ident) or (a.number is not None and a.number.endswith(ident))]
    if len(suffix) == 1:
        return suffix[0]
    if len(suffix) > 1:
        opts = ", ".join(a.masked for a in suffix)
        raise BrokerError(
            f"ambiguous account {ident!r} — matches more than one linked account "
            f"({opts}). Use the full account number or id."
        )

    opts = ", ".join(a.masked for a in accounts)
    raise BrokerError(f"no account matching {ident!r}; linked accounts: {opts}")


def status_str(status, default: str = "submitted") -> str:
    """Plain string form of an SDK order status.

    SDK enums stringify as "OrderStatus.REJECTED", which silently defeats
    downstream checks for "rejected" (the chase engine's place-failed path,
    _is_clean_send). Prefer the enum's .value so adapters always report the
    bare status word.
    """
    if status is None or status == "":
        return default
    return str(getattr(status, "value", status))


def first_present(*values):
    """First value that is not None.

    Balance fields need this instead of an `or` chain: a legitimate 0
    (zero buying power on a maxed-out margin account, zero NAV) is falsy
    and would silently fall through to a different — wrong — field.
    """
    for v in values:
        if v is not None:
            return v
    return None


@dataclass
class Balances:
    nav: Decimal
    cash: Decimal
    buying_power: Decimal


@runtime_checkable
class Broker(Protocol):
    """Contract every broker adapter must satisfy.

    Implementations live in `msts_trader/brokers/<name>.py` and register
    in `msts_trader/brokers/__init__.py`. Treat exceptions during normal
    flow as fatal: raise `BrokerError` for things the user should know,
    let everything else bubble up.
    """

    name: str
    account_id: str
    supports_fractional: bool
    supports_moc: bool
    # Adapters that can place/list/cancel GTC protective stop orders set
    # supports_stops = True and implement the three stop methods below.
    # Others leave it False (class attribute default works) — the CLI then
    # warns once and skips stop placement instead of failing the rebalance.
    supports_stops: bool = False
    # Adapters that can work a LIMIT order through the broker-agnostic
    # limit-chase engine (msts_trader/chase.py) set supports_limit_chase = True
    # and implement place_limit + order_status (and cancel_order, shared with
    # the stop API). Others leave it False — when the user asks for
    # --order-type limit-chase the CLI warns once and uses market orders.
    supports_limit_chase: bool = False

    def balances(self) -> Balances:
        """Net liquidating value, cash, equity buying power. Decimals throughout."""
        ...

    def positions(self) -> dict[str, Position]:
        """Open equity positions keyed by ticker. Empty dict if none."""
        ...

    def quote(self, tickers: Iterable[str]) -> dict[str, Decimal]:
        """Best-effort last/mark/mid per ticker. Missing keys = quote unavailable."""
        ...

    def place_market(self, order: Order, dry_run: bool = False) -> dict:
        """Submit a MARKET DAY order. Returns a flat dict with status + ids.

        If `order.moc` is set and the adapter declares supports_moc = True,
        submit a market-on-close order instead (fills in the closing
        auction). Adapters with supports_moc = False never see moc orders —
        the CLI refuses before placement.

        Required keys:  status (str), ticker (str)
        Suggested keys: order_id, side, quantity, reason (on errors), dry_run
        """
        ...

    # ---- Optional protective-stop API (supports_stops = True) ------------
    def place_stop(self, ticker: str, quantity: Decimal, stop_price: Decimal, dry_run: bool = False) -> dict:
        """Submit a GTC SELL STOP for an existing long. Same return contract
        as place_market."""
        raise NotImplementedError

    def open_stops(self) -> dict[str, list[dict]]:
        """Open stop orders keyed by ticker. Each item needs at least
        {order_id, quantity, stop_price}."""
        raise NotImplementedError

    def cancel_order(self, order_id: str) -> dict:
        """Cancel an open order by id. Shared by the stop API and the
        limit-chase engine. A dict with status 'error'/'rejected' (or a
        raised exception) signals the cancel FAILED — the chase engine then
        aborts rather than risk two live orders."""
        raise NotImplementedError

    # ---- Optional limit-chase API (supports_limit_chase = True) ----------
    def place_limit(self, order: Order, limit_price: Decimal, dry_run: bool = False) -> dict:
        """Submit a LIMIT DAY order at `limit_price`. Same return contract as
        place_market, with one extra REQUIREMENT for the chase engine: on a
        successful (non-dry-run) submit the result MUST carry a usable
        `order_id` — the engine polls and cancels by it, and aborts loudly if
        it's missing (an unidentifiable live order can't be managed safely).
        Whole-share rounding (where the broker requires it for limits) is the
        adapter's responsibility — return status 'skipped' if the size rounds
        away to nothing."""
        raise NotImplementedError

    def order_status(self, order_id: str) -> dict:
        """Normalized status of one order, driving the chase loop. Returns
        {status, filled_qty, filled_avg_price} where status is one of the
        constants in msts_trader.chase: WORKING, PARTIAL, FILLED, CANCELLED,
        REJECTED, UNKNOWN."""
        raise NotImplementedError

    # NOTE: `fills()` is an OPTIONAL capability, deliberately NOT part of this runtime_checkable
    # Protocol — adding it here would force every adapter to implement it or fail isinstance().
    # Adapters that can report average fill price (e.g. tastytrade) define `def fills(self) -> dict`
    # returning {ticker: avg_fill_price}; _execute calls it via hasattr() to fill-anchor stops.
    #
    # Same-login multi-account is also OPTIONAL (hasattr):
    #   list_linked_accounts() -> list[LinkedAccount]
    #   use_account(identifier: str) -> None
    # Brokers that only ever expose one account under a login still implement these so
    # --account / --all-accounts / multi `account =` work uniformly; single-account
    # adapters return a one-element list.
