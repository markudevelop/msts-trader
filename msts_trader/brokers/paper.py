"""Paper broker — simulates a $100k cash account locally. No real fills.

State is persisted to `~/.msts-trader/paper_state.json` so the simulated
NAV and positions evolve across sessions. Useful for dry-running the
flow without connecting any real brokerage.
"""
from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from ..models import Order, Position, Side
from .base import Balances, BrokerError

STATE_PATH = Path(os.path.expanduser("~/.msts-trader/paper_state.json"))
STARTING_CASH = Decimal("100000")


class Paper:
    name = "paper"
    supports_fractional = True
    supports_moc = True  # simulated: fills at the booked price, tagged moc
    supports_stops = True  # simulated GTC stops, persisted in paper state

    def __init__(self, starting_cash: str | float | Decimal | None = None):
        if not STATE_PATH.exists():
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            STATE_PATH.write_text(json.dumps({
                "cash": str(Decimal(str(starting_cash)) if starting_cash else STARTING_CASH),
                "positions": {},
                "last_prices": {},
            }))
        self.account_id = "PAPER"

    def _load(self) -> dict:
        return json.loads(STATE_PATH.read_text())

    def _save(self, state: dict) -> None:
        STATE_PATH.write_text(json.dumps(state, indent=2))

    def balances(self) -> Balances:
        s = self._load()
        cash = Decimal(s["cash"])
        equity = Decimal(0)
        for sym, qty_s in s["positions"].items():
            px = Decimal(s.get("last_prices", {}).get(sym, "0"))
            equity += Decimal(qty_s) * px
        nav = cash + equity
        return Balances(nav=nav, cash=cash, buying_power=cash)

    def positions(self) -> dict[str, Position]:
        s = self._load()
        out: dict[str, Position] = {}
        for sym, qty_s in s["positions"].items():
            qty = Decimal(qty_s)
            if qty == 0:
                continue
            px = Decimal(s.get("last_prices", {}).get(sym, "0"))
            out[sym] = Position(ticker=sym, quantity=qty, price=px)
        return out

    def quote(self, tickers: Iterable[str]) -> dict[str, Decimal]:
        """Paper broker cannot quote — caller must pre-seed via place_market notional.

        Returns whatever we last booked as last_price. Real flows seed quotes
        from the CSV path before calling diff; for paper testing, the CLI hands
        last_prices from a separate fetch (or the user supplies via env).
        """
        s = self._load()
        last = s.get("last_prices", {})
        out: dict[str, Decimal] = {}
        for t in {x.upper() for x in tickers}:
            v = last.get(t)
            if v:
                out[t] = Decimal(v)
        return out

    def set_quote(self, ticker: str, price: Decimal) -> None:
        """Test helper: explicitly set a quote in the paper book."""
        s = self._load()
        s.setdefault("last_prices", {})[ticker.upper()] = str(price)
        self._save(s)

    def place_market(self, order: Order, dry_run: bool = False) -> dict:
        # Normalise like quote()/set_quote() do, so a lowercase order ticker
        # can't book a position whose price lookup then misses last_prices.
        tkr = order.ticker.upper()
        qty = Decimal(str(round(float(order.quantity), 4)))
        if qty <= 0:
            return {"status": "skipped", "reason": "qty<=0", "ticker": tkr}
        px = order.estimated_price or Decimal(0)
        if px <= 0:
            return {"status": "error", "reason": "no price for paper fill", "ticker": tkr}
        if dry_run:
            return {"status": "dry-run", "ticker": tkr, "side": order.side.value, "quantity": float(qty), "dry_run": True}

        s = self._load()
        cash = Decimal(s["cash"])
        positions: dict[str, str] = dict(s.get("positions", {}))
        cur_qty = Decimal(positions.get(tkr, "0"))

        notional = qty * px
        if order.side == Side.BUY:
            if notional > cash + Decimal("1"):
                return {"status": "error", "reason": f"insufficient cash ${cash} < ${notional}", "ticker": tkr}
            cash -= notional
            new_qty = cur_qty + qty
        else:
            if cur_qty < qty:
                return {"status": "error", "reason": f"insufficient {tkr} ({cur_qty} < {qty})", "ticker": tkr}
            cash += notional
            new_qty = cur_qty - qty

        if new_qty == 0:
            positions.pop(tkr, None)
        else:
            positions[tkr] = str(new_qty)

        s["cash"] = str(cash)
        s["positions"] = positions
        last_prices = dict(s.get("last_prices", {}))
        last_prices[tkr] = str(px)
        s["last_prices"] = last_prices
        self._save(s)

        return {
            "status": "FILLED",
            "ticker": tkr,
            "side": order.side.value,
            "quantity": float(qty),
            "moc": order.moc,
            "order_id": f"paper-{tkr}-{int(qty * 100)}",
            "fill_price": float(px),
            "dry_run": False,
        }

    # ---- protective stops (simulated) ------------------------------------
    def place_stop(self, ticker: str, quantity: Decimal, stop_price: Decimal,
                   dry_run: bool = False) -> dict:
        tkr = ticker.upper()
        if dry_run:
            return {"status": "dry-run", "ticker": tkr, "stop_price": float(stop_price), "dry_run": True}
        s = self._load()
        stops = dict(s.get("stop_orders", {}))
        oid = f"paper-stop-{tkr}-{len(stops) + 1}"
        per = list(stops.get(tkr, []))
        per.append({"order_id": oid, "quantity": str(quantity), "stop_price": str(stop_price)})
        stops[tkr] = per
        s["stop_orders"] = stops
        self._save(s)
        return {"status": "ACCEPTED", "ticker": tkr, "order_id": oid,
                "stop_price": float(stop_price), "quantity": float(quantity), "dry_run": False}

    def open_stops(self) -> dict[str, list[dict]]:
        s = self._load()
        out: dict[str, list[dict]] = {}
        for tkr, lst in s.get("stop_orders", {}).items():
            out[tkr] = [{"order_id": o["order_id"], "quantity": Decimal(o["quantity"]),
                         "stop_price": Decimal(o["stop_price"])} for o in lst]
        return out

    def cancel_order(self, order_id: str) -> dict:
        s = self._load()
        stops = dict(s.get("stop_orders", {}))
        for tkr, lst in list(stops.items()):
            kept = [o for o in lst if o["order_id"] != order_id]
            if len(kept) != len(lst):
                if kept:
                    stops[tkr] = kept
                else:
                    stops.pop(tkr)
                s["stop_orders"] = stops
                self._save(s)
                return {"status": "CANCELLED", "order_id": order_id}
        return {"status": "error", "reason": "order not found", "order_id": order_id}

    def reset(self, starting_cash: Decimal | None = None) -> None:
        STATE_PATH.write_text(json.dumps({
            "cash": str(starting_cash or STARTING_CASH),
            "positions": {},
            "last_prices": {},
        }))
        if not starting_cash:
            return
        raise BrokerError("paper reset done")  # signal CLI to print confirmation
