from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Protocol, runtime_checkable

from ..models import Order, Position


class BrokerError(RuntimeError):
    """Anything the broker layer can't fix without user action."""


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

        Required keys:  status (str), ticker (str)
        Suggested keys: order_id, side, quantity, reason (on errors), dry_run
        """
        ...
