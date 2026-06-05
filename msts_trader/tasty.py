"""Tastytrade SDK wrapper — session, NAV, positions, market orders.

Lifted patterns from msts-live's `core/brokers/tastytrade_broker.py`. Single-
strategy only; no per-strategy ledger, no extended-hours chase fill in v1
(market-hours guard refuses to send during pre/after-hours instead).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from tastytrade import Account, Session
from tastytrade.instruments import Equity
from tastytrade.order import (
    InstrumentType,
    Leg,
    NewOrder,
    OrderAction,
    OrderTimeInForce,
    OrderType,
)

from .models import Order, Position, Side


@dataclass
class Balances:
    nav: Decimal
    cash: Decimal
    buying_power: Decimal


class Tasty:
    def __init__(self, provider_secret: str, refresh_token: str, account_id: str | None = None):
        if not provider_secret or not refresh_token:
            raise ValueError("provider_secret and refresh_token required")
        self._sess = Session(provider_secret, refresh_token)

        if account_id:
            self._acct = Account.get(self._sess, account_id)
            self._account_id = account_id
        else:
            accts = Account.get(self._sess)
            if isinstance(accts, list):
                if not accts:
                    raise RuntimeError("no accounts on this Tastytrade session")
                self._acct = accts[0]
            else:
                self._acct = accts
            self._account_id = self._acct.account_number

    @property
    def account_id(self) -> str:
        return self._account_id

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
        """Last/mark price per ticker. Uses Equity instrument metadata (no streamer).

        Tastytrade Equity objects expose `streamer_symbol` + price fields populated
        on fetch. Fast enough for ≤30 tickers; for bigger universes wire DXLinkStreamer.
        """
        tickers = list({t.upper() for t in tickers})
        out: dict[str, Decimal] = {}
        for t in tickers:
            try:
                eq = Equity.get(self._sess, t)
                # Tastytrade Equity exposes `lendability` + `tick_sizes`; price
                # is fetched via market-data REST. Fall back to a market-data call.
                px = self._last_price(t)
                if px is not None:
                    out[t] = px
            except Exception:
                continue
        return out

    def _last_price(self, ticker: str) -> Decimal | None:
        """Pull a single last price via the SDK's market-data helper."""
        try:
            from tastytrade.market_data import a_get_market_data, get_market_data  # type: ignore

            md = get_market_data(self._sess, [ticker], instrument_type=InstrumentType.EQUITY)
            for row in md:
                if getattr(row, "symbol", None) == ticker:
                    px = getattr(row, "last", None) or getattr(row, "mark", None) or getattr(row, "mid", None)
                    if px is not None:
                        return Decimal(str(px))
        except Exception:
            pass
        return None

    def place_market(self, order: Order, dry_run: bool = False) -> dict:
        """Submit a MARKET order. Returns a flat dict with status + fill info."""
        positions = self.positions()
        cur = positions.get(order.ticker)
        if order.side == Side.BUY:
            action = OrderAction.BUY_TO_CLOSE if cur and cur.quantity < 0 else OrderAction.BUY_TO_OPEN
        else:
            action = OrderAction.SELL_TO_CLOSE if cur and cur.quantity > 0 else OrderAction.SELL_TO_OPEN

        qty = Decimal(str(round(float(order.quantity), 2)))
        if qty <= 0:
            return {"status": "skipped", "reason": "qty<=0", "ticker": order.ticker}

        leg = Leg(
            instrument_type=InstrumentType.EQUITY,
            symbol=order.ticker,
            action=action,
            quantity=qty,
        )
        new_order = NewOrder(
            time_in_force=OrderTimeInForce.DAY,
            order_type=OrderType.MARKET,
            legs=[leg],
            price=None,
        )

        try:
            resp = self._acct.place_order(self._sess, new_order, dry_run=dry_run)
        except Exception as e:
            err = str(e).lower()
            if "fractional_trading_invalid_symbol" in err:
                whole = int(qty)
                if whole <= 0:
                    return {"status": "skipped", "reason": "fractional rejected, whole=0", "ticker": order.ticker}
                leg = Leg(
                    instrument_type=InstrumentType.EQUITY,
                    symbol=order.ticker,
                    action=action,
                    quantity=Decimal(whole),
                )
                new_order = NewOrder(
                    time_in_force=OrderTimeInForce.DAY,
                    order_type=OrderType.MARKET,
                    legs=[leg],
                    price=None,
                )
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
