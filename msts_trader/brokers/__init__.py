"""Broker registry. Add new brokers by importing them here.

Subscribers pick at runtime via `msts-trader --broker <name>` (or the
last-used default stored in the keychain).
"""
from __future__ import annotations

from .base import Balances, Broker, BrokerError

SUPPORTED = ("tastytrade", "alpaca", "ibkr", "schwab", "paper")


def make(name: str, **creds) -> Broker:
    """Instantiate a broker by name. Raises BrokerError if unknown."""
    name = name.lower().strip()
    if name == "tastytrade":
        from .tastytrade import Tastytrade
        return Tastytrade(**creds)
    if name == "alpaca":
        from .alpaca import Alpaca
        return Alpaca(**creds)
    if name == "ibkr":
        from .ibkr import IBKR
        return IBKR(**creds)
    if name == "schwab":
        from .schwab import Schwab
        return Schwab(**creds)
    if name == "paper":
        from .paper import Paper
        return Paper(**creds)
    raise BrokerError(f"unknown broker {name!r} — supported: {SUPPORTED}")


__all__ = ["Balances", "Broker", "BrokerError", "make", "SUPPORTED"]
