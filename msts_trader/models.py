from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class Target:
    ticker: str
    weight: Decimal


@dataclass
class Position:
    ticker: str
    quantity: Decimal
    price: Decimal

    @property
    def market_value(self) -> Decimal:
        return self.quantity * self.price


@dataclass
class Order:
    ticker: str
    side: Side
    quantity: Decimal
    estimated_price: Decimal | None = None
    notional: Decimal = Decimal(0)


@dataclass
class RebalanceRow:
    ticker: str
    current_pct: Decimal
    target_pct: Decimal
    delta_dollars: Decimal
    order: Order | None
    note: str = ""


@dataclass
class Preview:
    nav: Decimal
    buying_power: Decimal
    cash: Decimal
    rows: list[RebalanceRow]
    orders: list[Order]
    warnings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    @property
    def has_blockers(self) -> bool:
        return bool(self.blockers)
