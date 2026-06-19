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
    supports_moc = False  # tastytrade's API has no closing-auction order type
    supports_stops = True  # GTC SELL STOP via OrderType.STOP + stop_trigger
    supports_limit_chase = True  # LIMIT DAY via the chase engine (whole shares)

    def __init__(self, provider_secret: str, refresh_token: str, account_id: str | None = None, is_test: bool = False):
        if not provider_secret or not refresh_token:
            raise BrokerError("provider_secret and refresh_token required")
        # is_test=True targets Tastytrade's certification (sandbox) environment;
        # cert-issued OAuth keys are rejected by production and vice versa.
        self.is_test = is_test
        self._sess = Session(provider_secret, refresh_token, is_test=is_test)

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

    def margin_requirement(self, orders) -> Decimal | None:
        """Real total buying-power requirement for the given BUY orders.

        Dry-runs each buy and sums the broker's reported
        change_in_buying_power — this captures leveraged-ETF margin rates
        (e.g. TBT, EDZ) that a notional estimate misses. Returns None if any
        dry-run fails (e.g. market closed), so the caller falls back to the
        notional approximation rather than sizing on partial data.
        """
        total = Decimal(0)
        for o in orders:
            if o.side != Side.BUY:
                continue
            qty = Decimal(str(round(float(o.quantity), 2)))
            if qty <= 0:
                continue
            leg = Leg(instrument_type=InstrumentType.EQUITY, symbol=o.ticker, action=OrderAction.BUY_TO_OPEN, quantity=qty)
            new_order = NewOrder(time_in_force=OrderTimeInForce.DAY, order_type=OrderType.MARKET, legs=[leg], price=None)
            try:
                resp = self._acct.place_order(self._sess, new_order, dry_run=True)
            except Exception:
                return None
            bpe = getattr(resp, "buying_power_effect", None)
            chg = getattr(bpe, "change_in_buying_power", None) if bpe is not None else None
            if chg is None:
                return None
            total += abs(Decimal(str(chg)))
        return total

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
                qty = Decimal(whole)  # report the quantity actually submitted, not the rejected fractional one
                leg = Leg(instrument_type=InstrumentType.EQUITY, symbol=order.ticker, action=action, quantity=qty)
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

    # ---- limit chase ------------------------------------------------------
    def place_limit(self, order: Order, limit_price: Decimal, dry_run: bool = False) -> dict:
        """LIMIT DAY order for the chase engine. Tastytrade LIMIT orders need
        WHOLE shares (fractional rejects with "must be a positive number"), and
        the price is signed: BUY is a debit (negative), SELL a credit."""
        positions = self.positions()
        cur = positions.get(order.ticker)
        if order.side == Side.BUY:
            action = OrderAction.BUY_TO_CLOSE if cur and cur.quantity < 0 else OrderAction.BUY_TO_OPEN
        else:
            action = OrderAction.SELL_TO_CLOSE if cur and cur.quantity > 0 else OrderAction.SELL_TO_OPEN

        qty = Decimal(int(Decimal(str(order.quantity))))
        if qty <= 0:
            return {"status": "skipped", "reason": "limit qty rounds to <1 share",
                    "ticker": order.ticker}
        px = Decimal(str(limit_price)).quantize(Decimal("0.01"))
        signed = -abs(px) if order.side == Side.BUY else abs(px)
        leg = Leg(instrument_type=InstrumentType.EQUITY, symbol=order.ticker,
                  action=action, quantity=qty)
        new_order = NewOrder(time_in_force=OrderTimeInForce.DAY, order_type=OrderType.LIMIT,
                             legs=[leg], price=signed)
        try:
            resp = self._acct.place_order(self._sess, new_order, dry_run=dry_run)
        except Exception as e:
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
            "limit_price": float(px),
            "dry_run": dry_run,
        }

    def order_status(self, order_id) -> dict:
        """Normalized {status, filled_qty, filled_avg_price}. Aggregates fills
        from each leg (tastytrade>=11 hides top-level fill totals), and treats a
        fully-drained order as FILLED even if the /orders view still lags."""
        from ..chase import CANCELLED, FILLED, PARTIAL, REJECTED, UNKNOWN, WORKING

        try:
            o = self._acct.get_order(self._sess, int(order_id))
        except Exception as e:
            return {"status": UNKNOWN, "filled_qty": 0.0, "filled_avg_price": None,
                    "reason": str(e)}
        legs = getattr(o, "legs", None) or []
        total_filled = 0.0
        total_cost = 0.0
        for leg in legs:
            for fill in getattr(leg, "fills", None) or []:
                try:
                    q = float(fill.quantity or 0)
                    p = float(fill.fill_price or 0)
                except Exception:
                    continue
                total_filled += q
                total_cost += q * p
        avg = (total_cost / total_filled) if total_filled > 0 else None

        raw = str(getattr(o, "status", "")).lower()
        if "fill" in raw and "partial" not in raw:
            status = FILLED
        elif "partial" in raw:
            status = PARTIAL
        elif "reject" in raw:
            status = REJECTED
        elif any(k in raw for k in ("cancel", "expired")):
            status = CANCELLED
        elif any(k in raw for k in ("live", "received", "routed", "contingent", "in_flight")):
            status = WORKING
        else:
            status = UNKNOWN
        # /orders/live is eventually consistent: if every leg has drained, the
        # fill happened even when the top-level status still reads LIVE.
        if legs and total_filled > 0 and all(
                float(getattr(leg, "remaining_quantity", 0) or 0) == 0 for leg in legs):
            status = FILLED
        return {"status": status, "filled_qty": total_filled, "filled_avg_price": avg}

    # ---- protective stops -------------------------------------------------
    def place_stop(self, ticker: str, quantity: Decimal, stop_price: Decimal,
                   dry_run: bool = False) -> dict:
        """GTC SELL STOP for an existing long. Stop orders must be whole-share
        on tastytrade — fractional quantity is rounded DOWN (the residual
        fraction stays unprotected rather than over-selling)."""
        qty = Decimal(int(quantity))
        if qty <= 0:
            return {"status": "skipped", "reason": "whole-share qty rounds to 0",
                    "ticker": ticker}
        leg = Leg(instrument_type=InstrumentType.EQUITY, symbol=ticker,
                  action=OrderAction.SELL_TO_CLOSE, quantity=qty)
        new_order = NewOrder(time_in_force=OrderTimeInForce.GTC,
                             order_type=OrderType.STOP, legs=[leg],
                             stop_trigger=stop_price, price=None)
        try:
            resp = self._acct.place_order(self._sess, new_order, dry_run=dry_run)
        except Exception as e:
            return {"status": "error", "reason": str(e), "ticker": ticker}
        order_obj = getattr(resp, "order", None)
        return {
            "status": str(getattr(order_obj, "status", None) or "submitted"),
            "ticker": ticker,
            "order_id": getattr(order_obj, "id", None),
            "quantity": float(qty),
            "stop_price": float(stop_price),
            "dry_run": dry_run,
        }

    def open_stops(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for o in self._acct.get_live_orders(self._sess):
            if getattr(o, "order_type", None) != OrderType.STOP:
                continue
            status = str(getattr(o, "status", "")).lower()
            if any(k in status for k in ("cancel", "reject", "filled", "expired")):
                continue
            legs = getattr(o, "legs", None) or []
            if not legs:
                continue
            tkr = getattr(legs[0], "symbol", None)
            if not tkr:
                continue
            out.setdefault(tkr, []).append({
                "order_id": getattr(o, "id", None),
                "quantity": Decimal(str(getattr(legs[0], "quantity", 0) or 0)),
                "stop_price": Decimal(str(getattr(o, "stop_trigger", 0) or 0)),
            })
        return out

    def fills(self) -> dict:
        """Average fill price per ticker from today's FILLED buy orders, so a protective stop can be
        anchored on the REAL entry (not the pre-trade quote). Empty for names not yet filled."""
        out: dict = {}
        for o in self._acct.get_live_orders(self._sess):
            if "filled" not in str(getattr(o, "status", "")).lower():
                continue
            legs = getattr(o, "legs", None) or []
            if not legs or "buy" not in str(getattr(legs[0], "action", "")).lower():
                continue
            fl = getattr(legs[0], "fills", None) or []
            try:
                q = sum(Decimal(str(x.quantity)) for x in fl)
                n = sum(Decimal(str(x.quantity)) * Decimal(str(x.fill_price)) for x in fl)
                if q > 0:
                    out[getattr(legs[0], "symbol", None)] = n / q
            except Exception:
                continue
        return out

    def cancel_order(self, order_id) -> dict:
        try:
            self._acct.delete_order(self._sess, int(order_id))
            return {"status": "CANCELLED", "order_id": order_id}
        except Exception as e:
            return {"status": "error", "reason": str(e), "order_id": order_id}
