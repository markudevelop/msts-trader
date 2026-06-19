"""Alpaca adapter — API key / secret, live or paper toggle.

Built on the public `alpaca-py` SDK (https://pypi.org/project/alpaca-py/).
Fractional shares supported on MARKET orders.
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
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import (
        GetOrdersRequest,
        LimitOrderRequest,
        MarketOrderRequest,
        StopOrderRequest,
    )
    _ALPACA_OK = True
except ImportError:
    _ALPACA_OK = False


class Alpaca:
    name = "alpaca"
    supports_fractional = True
    supports_moc = True  # TimeInForce.CLS — whole shares only
    supports_stops = True  # GTC SELL STOP via StopOrderRequest (whole shares)
    supports_limit_chase = True  # LIMIT DAY via LimitOrderRequest

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
        if order.moc:
            # Alpaca only accepts whole shares with TimeInForce.CLS.
            qty = float(int(qty))
        if qty <= 0:
            reason = "qty rounds to 0 (MOC requires whole shares)" if order.moc else "qty<=0"
            return {"status": "skipped", "reason": reason, "ticker": order.ticker}
        if dry_run:
            return {
                "status": "dry-run",
                "ticker": order.ticker,
                "side": order.side.value,
                "quantity": qty,
                "moc": order.moc,
                "dry_run": True,
            }
        side = OrderSide.BUY if order.side == Side.BUY else OrderSide.SELL
        req = MarketOrderRequest(
            symbol=order.ticker,
            qty=qty,
            side=side,
            time_in_force=TimeInForce.CLS if order.moc else TimeInForce.DAY,
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
            "moc": order.moc,
            "order_id": str(oid) if (oid := getattr(resp, "id", None)) is not None else None,
            "dry_run": False,
        }

    # ---- limit chase ------------------------------------------------------
    def place_limit(self, order: Order, limit_price: Decimal, dry_run: bool = False) -> dict:
        """LIMIT DAY order for the chase engine. Alpaca accepts fractional
        limit quantities with DAY TIF; if it rejects one, the engine's
        place-failed path falls back to a market order."""
        qty = round(float(order.quantity), 4)
        if qty <= 0:
            return {"status": "skipped", "reason": "qty<=0", "ticker": order.ticker}
        px = round(float(limit_price), 2)
        if dry_run:
            return {"status": "dry-run", "ticker": order.ticker, "side": order.side.value,
                    "quantity": qty, "limit_price": px, "dry_run": True}
        side = OrderSide.BUY if order.side == Side.BUY else OrderSide.SELL
        req = LimitOrderRequest(symbol=order.ticker, qty=qty, side=side,
                                time_in_force=TimeInForce.DAY, limit_price=px)
        try:
            resp = self._client.submit_order(req)
        except Exception as e:
            return {"status": "error", "reason": str(e), "ticker": order.ticker}
        return {
            "status": str(getattr(resp, "status", "submitted")),
            "ticker": order.ticker,
            "side": order.side.value,
            "quantity": qty,
            "order_id": str(oid) if (oid := getattr(resp, "id", None)) is not None else None,
            "limit_price": px,
            "dry_run": False,
        }

    def order_status(self, order_id: str) -> dict:
        from ..chase import CANCELLED, FILLED, PARTIAL, REJECTED, UNKNOWN, WORKING

        try:
            o = self._client.get_order_by_id(order_id)
        except Exception as e:
            return {"status": UNKNOWN, "filled_qty": 0.0, "filled_avg_price": None,
                    "reason": str(e)}
        st = getattr(o, "status", None)
        # alpaca-py returns an OrderStatus enum (str() -> "OrderStatus.FILLED");
        # prefer .value ("filled") so the match below works either way.
        raw = (st.value if hasattr(st, "value") else str(st or "")).lower()
        if raw == "filled":
            status = FILLED
        elif raw == "partially_filled":
            status = PARTIAL
        elif raw == "rejected":
            status = REJECTED
        elif raw in ("canceled", "cancelled", "expired", "done_for_day", "stopped"):
            status = CANCELLED
        elif raw in ("new", "accepted", "pending_new", "accepted_for_bidding",
                     "pending_replace", "replaced", "calculated"):
            status = WORKING
        else:
            status = UNKNOWN
        filled = float(getattr(o, "filled_qty", 0) or 0)
        avg = getattr(o, "filled_avg_price", None)
        return {"status": status, "filled_qty": filled,
                "filled_avg_price": float(avg) if avg else None}

    # ---- protective stops -------------------------------------------------
    def place_stop(self, ticker: str, quantity: Decimal, stop_price: Decimal,
                   dry_run: bool = False) -> dict:
        """GTC SELL STOP for an existing long. Alpaca stop orders are
        whole-share only — fractional quantity rounds DOWN (residual fraction
        stays unprotected rather than over-selling)."""
        qty = int(quantity)
        if qty <= 0:
            return {"status": "skipped", "reason": "whole-share qty rounds to 0",
                    "ticker": ticker}
        if dry_run:
            return {"status": "dry-run", "ticker": ticker,
                    "stop_price": float(stop_price), "dry_run": True}
        req = StopOrderRequest(symbol=ticker, qty=qty, side=OrderSide.SELL,
                               time_in_force=TimeInForce.GTC,
                               stop_price=round(float(stop_price), 2))
        try:
            resp = self._client.submit_order(req)
        except Exception as e:
            return {"status": "error", "reason": str(e), "ticker": ticker}
        return {"status": str(getattr(resp, "status", "submitted")),
                "ticker": ticker, "order_id": str(getattr(resp, "id", "")),
                "quantity": float(qty), "stop_price": float(stop_price),
                "dry_run": False}

    def open_stops(self) -> dict[str, list[dict]]:
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)
        out: dict[str, list[dict]] = {}
        for o in self._client.get_orders(req):
            if str(getattr(o, "order_type", "")).lower() != "stop":
                continue
            tkr = getattr(o, "symbol", None)
            if not tkr:
                continue
            out.setdefault(tkr, []).append({
                "order_id": str(o.id),
                "quantity": Decimal(str(o.qty or 0)),
                "stop_price": Decimal(str(o.stop_price or 0)),
            })
        return out

    def cancel_order(self, order_id: str) -> dict:
        try:
            self._client.cancel_order_by_id(order_id)
            return {"status": "CANCELLED", "order_id": order_id}
        except Exception as e:
            return {"status": "error", "reason": str(e), "order_id": order_id}
