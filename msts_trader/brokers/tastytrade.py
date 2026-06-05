"""Tastytrade adapter — session, NAV, positions, market orders.

Built on the public `tastytrade` Python SDK (https://pypi.org/project/tastytrade/).
The rebalance flow refuses to send outside RTH instead of routing
extended-hours limit chases.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from tastytrade import Account, Session
from tastytrade.order import (
    InstrumentType,
    Leg,
    NewOrder,
    OrderAction,
    OrderTimeInForce,
    OrderType,
)

from ..models import Order, Position, Side
from .base import Balances, BrokerError


class Tastytrade:
    name = "tastytrade"
    supports_fractional = True  # MARKET orders only

    def __init__(self, provider_secret: str, refresh_token: str, account_id: str | None = None):
        if not provider_secret or not refresh_token:
            raise BrokerError("provider_secret and refresh_token required")
        self._sess = Session(provider_secret, refresh_token)

        if account_id:
            self._acct = Account.get(self._sess, account_id)
            self.account_id = account_id
        else:
            accts = Account.get(self._sess)
            if isinstance(accts, list):
                if not accts:
                    raise BrokerError("no accounts on this Tastytrade session")
                self._acct = accts[0]
            else:
                self._acct = accts
            self.account_id = self._acct.account_number

    def balances(self) -> Balances:
        b = self._acct.get_balances(self._sess)
        nav = Decimal(str(b.net_liquidating_value or 0))
        cash = Decimal(str(b.cash_balance or 0))
        bp = Decimal(str(b.equity_buying_power or b.derivative_buying_power or 0))
        return Balances(nav=nav, cash=cash, buying_power=bp)

    def positions(self) -> dict[str, Position]:
        raw = self._acct.get_positions(self._sess)
        out: dict[str, Position] = {}
        for p in raw:
            if getattr(p, "instrument_type", None) != "Equity":
                continue
            qty = Decimal(str(p.quantity))
            price = Decimal(str(getattr(p, "close_price", None) or getattr(p, "mark", None) or 0))
            if qty == 0:
                continue
            out[p.symbol] = Position(ticker=p.symbol, quantity=qty, price=price)
        return out

    def quote(self, tickers: Iterable[str]) -> dict[str, Decimal]:
        """Last/mark price per ticker, batched via the SDK's market-data API."""
        symbols = sorted({t.upper() for t in tickers})
        if not symbols:
            return {}
        try:
            from tastytrade.market_data import get_market_data_by_type  # type: ignore

            rows = get_market_data_by_type(self._sess, equities=symbols)
        except Exception:
            # Fall back to one-by-one if the batch API isn't available
            return self._quote_single(symbols)

        out: dict[str, Decimal] = {}
        for row in rows or []:
            sym = getattr(row, "symbol", None)
            if not sym:
                continue
            px = self._extract_price(row)
            if px is not None:
                out[sym] = px
        return out

    def _quote_single(self, symbols: list[str]) -> dict[str, Decimal]:
        try:
            from tastytrade.market_data import get_market_data  # type: ignore
        except Exception:
            return {}
        out: dict[str, Decimal] = {}
        for sym in symbols:
            try:
                md = get_market_data(self._sess, sym, InstrumentType.EQUITY)
            except Exception:
                continue
            px = self._extract_price(md) if md else None
            if px is not None:
                out[sym] = px
        return out

    @staticmethod
    def _extract_price(row) -> Decimal | None:
        for attr in ("last", "mark", "mid", "close"):
            v = getattr(row, attr, None)
            if v is None:
                continue
            try:
                d = Decimal(str(v))
            except Exception:
                continue
            if d > 0:
                return d
        return None

    def place_market(self, order: Order, dry_run: bool = False) -> dict:
        positions = self.positions()
        cur = positions.get(order.ticker)
        if order.side == Side.BUY:
            action = OrderAction.BUY_TO_CLOSE if cur and cur.quantity < 0 else OrderAction.BUY_TO_OPEN
        else:
            action = OrderAction.SELL_TO_CLOSE if cur and cur.quantity > 0 else OrderAction.SELL_TO_OPEN

        qty = Decimal(str(round(float(order.quantity), 2)))
        if qty <= 0:
            return {"status": "skipped", "reason": "qty<=0", "ticker": order.ticker}

        leg = Leg(instrument_type=InstrumentType.EQUITY, symbol=order.ticker, action=action, quantity=qty)
        new_order = NewOrder(time_in_force=OrderTimeInForce.DAY, order_type=OrderType.MARKET, legs=[leg], price=None)

        try:
            resp = self._acct.place_order(self._sess, new_order, dry_run=dry_run)
        except Exception as e:
            err = str(e).lower()
            if "fractional_trading_invalid_symbol" in err:
                whole = int(qty)
                if whole <= 0:
                    return {"status": "skipped", "reason": "fractional rejected, whole=0", "ticker": order.ticker}
                leg = Leg(instrument_type=InstrumentType.EQUITY, symbol=order.ticker, action=action, quantity=Decimal(whole))
                new_order = NewOrder(time_in_force=OrderTimeInForce.DAY, order_type=OrderType.MARKET, legs=[leg], price=None)
                resp = self._acct.place_order(self._sess, new_order, dry_run=dry_run)
            else:
                return {"status": "error", "reason": str(e), "ticker": order.ticker}

        order_obj = getattr(resp, "order", None)
        order_id = getattr(order_obj, "id", None) or getattr(resp, "id", None)
        status = getattr(order_obj, "status", None) or getattr(resp, "status", None) or "submitted"
        return {
            "status": str(status),
            "ticker": order.ticker,
            "side": order.side.value,
            "quantity": float(qty),
            "order_id": order_id,
            "dry_run": dry_run,
        }
