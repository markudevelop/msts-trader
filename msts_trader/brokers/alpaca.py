"""Alpaca adapter — API key / secret, live or paper toggle.

Uses the `alpaca-py` SDK. Lifted patterns from msts-live's
`core/brokers/alpaca_broker.py`. Fractional shares supported.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from ..models import Order, Position, Side
from .base import Balances, BrokerError

try:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestQuoteRequest
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest
    _ALPACA_OK = True
except ImportError:
    _ALPACA_OK = False


class Alpaca:
    name = "alpaca"
    supports_fractional = True

    def __init__(self, api_key: str, secret_key: str, paper: bool = False):
        if not _ALPACA_OK:
            raise BrokerError("alpaca-py not installed. Run: pip install alpaca-py")
        if not api_key or not secret_key:
            raise BrokerError("api_key and secret_key required")
        self._paper = bool(paper)
        self._client = TradingClient(api_key=api_key, secret_key=secret_key, paper=self._paper)
        self._data = StockHistoricalDataClient(api_key=api_key, secret_key=secret_key)
        acct = self._client.get_account()
        self.account_id = acct.account_number

    def balances(self) -> Balances:
        a = self._client.get_account()
        return Balances(
            nav=Decimal(str(a.equity or 0)),
            cash=Decimal(str(a.cash or 0)),
            buying_power=Decimal(str(a.buying_power or 0)),
        )

    def positions(self) -> dict[str, Position]:
        out: dict[str, Position] = {}
        for p in self._client.get_all_positions():
            qty = Decimal(str(p.qty))
            if qty == 0:
                continue
            out[p.symbol] = Position(
                ticker=p.symbol,
                quantity=qty,
                price=Decimal(str(p.current_price or 0)),
            )
        return out

    def quote(self, tickers: Iterable[str]) -> dict[str, Decimal]:
        symbols = sorted({t.upper() for t in tickers})
        if not symbols:
            return {}
        try:
            req = StockLatestQuoteRequest(symbol_or_symbols=symbols)
            quotes = self._data.get_stock_latest_quote(req)
        except Exception:
            return {}
        out: dict[str, Decimal] = {}
        for sym, q in quotes.items():
            mid = None
            ap = getattr(q, "ask_price", None)
            bp = getattr(q, "bid_price", None)
            if ap and bp and float(ap) > 0 and float(bp) > 0:
                mid = (Decimal(str(ap)) + Decimal(str(bp))) / Decimal(2)
            elif ap and float(ap) > 0:
                mid = Decimal(str(ap))
            elif bp and float(bp) > 0:
                mid = Decimal(str(bp))
            if mid is not None and mid > 0:
                out[sym] = mid
        return out

    def place_market(self, order: Order, dry_run: bool = False) -> dict:
        qty = round(float(order.quantity), 4)
        if qty <= 0:
            return {"status": "skipped", "reason": "qty<=0", "ticker": order.ticker}
        if dry_run:
            return {
                "status": "dry-run",
                "ticker": order.ticker,
                "side": order.side.value,
                "quantity": qty,
                "dry_run": True,
            }
        side = OrderSide.BUY if order.side == Side.BUY else OrderSide.SELL
        req = MarketOrderRequest(
            symbol=order.ticker,
            qty=qty,
            side=side,
            time_in_force=TimeInForce.DAY,
        )
        try:
            resp = self._client.submit_order(req)
        except Exception as e:
            return {"status": "error", "reason": str(e), "ticker": order.ticker}
        return {
            "status": str(getattr(resp, "status", "submitted")),
            "ticker": order.ticker,
            "side": order.side.value,
            "quantity": qty,
            "order_id": str(getattr(resp, "id", "")) or None,
            "dry_run": False,
        }
