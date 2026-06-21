"""Tradier adapter — REST + bearer token (no extra dependency, stdlib only).

Built on Tradier's documented Brokerage REST API
(https://documentation.tradier.com). Works against the production or the
free sandbox endpoint, so you can test end-to-end without risking money.

Auth (env or creds-file):
  TRADIER_ACCESS_TOKEN   bearer token (sandbox or production)
  TRADIER_ACCOUNT_ID     account number (optional; auto-discovered if absent)
  TRADIER_SANDBOX        "1" to use the sandbox endpoint

Equity market orders are whole-share (Tradier does not do fractional).
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from decimal import Decimal
from typing import Iterable

from ..models import Order, Position, Side
from .base import Balances, BrokerError, first_present

PROD_BASE = "https://api.tradier.com"
SANDBOX_BASE = "https://sandbox.tradier.com"


class Tradier:
    name = "tradier"
    supports_fractional = False
    supports_moc = False  # Tradier's API has no closing-auction order type
    supports_stops = True  # GTC sell stop via type=stop
    supports_limit_chase = True  # LIMIT DAY via type=limit (whole shares)

    def __init__(self, access_token: str, account_id: str | None = None, sandbox: bool = False, timeout: float = 20.0):
        if not access_token:
            raise BrokerError("access_token required")
        self._token = access_token
        self._base = SANDBOX_BASE if sandbox else PROD_BASE
        self._timeout = float(timeout)
        self.account_id = account_id or self._discover_account_id()

    # ----- HTTP -----

    def _request(self, method: str, path: str, params: dict | None = None) -> dict:
        url = self._base + path
        data = None
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if method == "GET" and params:
            url += "?" + urllib.parse.urlencode(params)
        elif method == "POST":
            data = urllib.parse.urlencode(params or {}).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310 (fixed host)
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:300]
            raise BrokerError(f"Tradier {e.code}: {detail}") from e
        except Exception as e:
            raise BrokerError(f"Tradier request failed: {e}") from e
        return json.loads(body) if body.strip() else {}

    def _discover_account_id(self) -> str:
        prof = self._request("GET", "/v1/user/profile").get("profile") or {}
        acct = prof.get("account")
        if isinstance(acct, list):
            if not acct:
                raise BrokerError("Tradier profile has no accounts")
            return str(acct[0]["account_number"])
        if isinstance(acct, dict):
            return str(acct["account_number"])
        raise BrokerError("could not resolve a Tradier account number")

    # ----- Broker protocol -----

    def balances(self) -> Balances:
        b = self._request("GET", f"/v1/accounts/{self.account_id}/balances").get("balances") or {}
        nav = Decimal(str(b.get("total_equity") or 0))
        cash = Decimal(str(b.get("total_cash") or 0))
        # first_present, not `or`: stock_buying_power of 0 (maxed-out margin)
        # must not fall through to cash_available and report phantom BP.
        bp = first_present(
            (b.get("margin") or {}).get("stock_buying_power"),
            (b.get("cash") or {}).get("cash_available"),
            b.get("total_cash"),
            0,
        )
        return Balances(nav=nav, cash=cash, buying_power=Decimal(str(bp)))

    def positions(self) -> dict[str, Position]:
        raw = self._request("GET", f"/v1/accounts/{self.account_id}/positions").get("positions")
        if not raw or raw == "null":
            return {}
        items = raw.get("position")
        if items is None:
            return {}
        if isinstance(items, dict):
            items = [items]
        out: dict[str, Position] = {}
        for p in items:
            sym = p.get("symbol")
            qty = Decimal(str(p.get("quantity") or 0))
            if not sym or qty == 0:
                continue
            cost = Decimal(str(p.get("cost_basis") or 0))
            avg = (cost / qty) if qty else Decimal(0)  # Tradier omits live price; use avg cost
            out[sym] = Position(ticker=sym, quantity=qty, price=avg)
        return out

    def quote(self, tickers: Iterable[str]) -> dict[str, Decimal]:
        symbols = sorted({t.upper() for t in tickers})
        if not symbols:
            return {}
        data = self._request("GET", "/v1/markets/quotes", {"symbols": ",".join(symbols)})
        q = (data.get("quotes") or {}).get("quote")
        if q is None:
            return {}
        if isinstance(q, dict):
            q = [q]
        out: dict[str, Decimal] = {}
        for item in q:
            sym = item.get("symbol")
            px = item.get("last") or item.get("close") or item.get("bid") or item.get("ask")
            if sym and px and float(px) > 0:
                out[sym] = Decimal(str(px))
        return out

    def margin_requirement(self, orders) -> Decimal | None:
        """Real total margin requirement for the BUY orders via Tradier's
        order preview (margin_change, or cost as a fallback). Returns None if
        any preview fails (caller falls back to notional)."""
        total = Decimal(0)
        for o in orders:
            if o.side != Side.BUY:
                continue
            qty = int(o.quantity)
            if qty <= 0:
                continue
            params = {
                "class": "equity",
                "symbol": o.ticker,
                "side": "buy",
                "quantity": qty,
                "type": "market",
                "duration": "day",
                "preview": "true",
            }
            try:
                resp = self._request("POST", f"/v1/accounts/{self.account_id}/orders", params)
            except Exception:
                return None
            od = resp.get("order") or {}
            mc = od.get("margin_change")
            if mc is None:
                mc = od.get("cost")
            if mc is None:
                return None
            total += abs(Decimal(str(mc)))
        return total

    def place_market(self, order: Order, dry_run: bool = False) -> dict:
        qty = int(order.quantity)  # whole shares
        if qty <= 0:
            return {"status": "skipped", "reason": "qty rounds to 0 (Tradier whole shares)", "ticker": order.ticker}
        side = "buy" if order.side == Side.BUY else "sell"
        params = {
            "class": "equity",
            "symbol": order.ticker,
            "side": side,
            "quantity": qty,
            "type": "market",
            "duration": "day",
        }
        if dry_run:
            params["preview"] = "true"
        resp = self._request("POST", f"/v1/accounts/{self.account_id}/orders", params)
        o = resp.get("order") or {}
        if dry_run:
            return {
                "status": "dry-run",
                "ticker": order.ticker,
                "side": order.side.value,
                "quantity": qty,
                "dry_run": True,
                "preview": o,
            }
        status = o.get("status") or "submitted"
        if str(status).lower() in ("rejected", "error"):
            return {"status": "error", "reason": json.dumps(o)[:300], "ticker": order.ticker}
        return {
            "status": str(status),
            "ticker": order.ticker,
            "side": order.side.value,
            "quantity": qty,
            "order_id": str(o.get("id")) if o.get("id") is not None else None,
            "dry_run": False,
        }

    # ---- limit chase ------------------------------------------------------
    def place_limit(self, order: Order, limit_price: Decimal, dry_run: bool = False) -> dict:
        """LIMIT DAY order for the chase engine. Tradier equities are
        whole-share — a size that rounds to 0 is skipped (the engine then
        market-fallbacks the dust)."""
        qty = int(order.quantity)
        if qty <= 0:
            return {"status": "skipped", "reason": "qty rounds to 0 (Tradier whole shares)", "ticker": order.ticker}
        side = "buy" if order.side == Side.BUY else "sell"
        params = {
            "class": "equity",
            "symbol": order.ticker,
            "side": side,
            "quantity": qty,
            "type": "limit",
            "duration": "day",
            "price": f"{float(limit_price):.2f}",
        }
        if dry_run:
            params["preview"] = "true"
            resp = self._request("POST", f"/v1/accounts/{self.account_id}/orders", params)
            return {
                "status": "dry-run",
                "ticker": order.ticker,
                "side": order.side.value,
                "quantity": qty,
                "limit_price": float(limit_price),
                "dry_run": True,
                "preview": resp.get("order") or {},
            }
        resp = self._request("POST", f"/v1/accounts/{self.account_id}/orders", params)
        o = resp.get("order") or {}
        status = o.get("status") or "submitted"
        if str(status).lower() in ("rejected", "error"):
            return {"status": "error", "reason": json.dumps(o)[:300], "ticker": order.ticker}
        return {
            "status": str(status),
            "ticker": order.ticker,
            "side": order.side.value,
            "quantity": qty,
            "order_id": str(o.get("id")) if o.get("id") is not None else None,
            "limit_price": float(limit_price),
            "dry_run": False,
        }

    def order_status(self, order_id) -> dict:
        from ..chase import CANCELLED, FILLED, PARTIAL, REJECTED, UNKNOWN, WORKING

        try:
            resp = self._request("GET", f"/v1/accounts/{self.account_id}/orders/{order_id}", None)
        except Exception as e:
            return {"status": UNKNOWN, "filled_qty": 0.0, "filled_avg_price": None, "reason": str(e)}
        o = resp.get("order") or {}
        raw = str(o.get("status", "")).lower()
        if raw == "filled":
            status = FILLED
        elif raw == "partially_filled":
            status = PARTIAL
        elif raw == "rejected":
            status = REJECTED
        elif raw in ("canceled", "cancelled", "expired"):
            status = CANCELLED
        elif raw in ("open", "pending"):
            status = WORKING
        else:
            status = UNKNOWN
        filled = float(o.get("exec_quantity") or 0)
        avg = o.get("avg_fill_price")
        return {
            "status": status,
            "filled_qty": filled,
            "filled_avg_price": float(avg) if avg and float(avg) > 0 else None,
        }

    # ---- protective stops -------------------------------------------------
    def place_stop(self, ticker: str, quantity, stop_price, dry_run: bool = False) -> dict:
        qty = int(quantity)
        if qty <= 0:
            return {"status": "skipped", "reason": "whole-share qty rounds to 0", "ticker": ticker}
        params = {
            "class": "equity",
            "symbol": ticker,
            "side": "sell",
            "quantity": qty,
            "type": "stop",
            "duration": "gtc",
            "stop": f"{float(stop_price):.2f}",
        }
        if dry_run:
            return {"status": "dry-run", "ticker": ticker, "stop_price": float(stop_price), "dry_run": True}
        resp = self._request("POST", f"/v1/accounts/{self.account_id}/orders", params)
        o = resp.get("order") or {}
        return {
            "status": str(o.get("status") or "submitted"),
            "ticker": ticker,
            "order_id": str(o.get("id", "")),
            "quantity": qty,
            "stop_price": float(stop_price),
            "dry_run": False,
        }

    def open_stops(self) -> dict:
        resp = self._request("GET", f"/v1/accounts/{self.account_id}/orders", None)
        raw = resp.get("orders") or {}
        items = raw.get("order") if isinstance(raw, dict) else None
        if items is None:
            return {}
        if isinstance(items, dict):
            items = [items]
        out: dict = {}
        for o in items:
            if str(o.get("type", "")).lower() != "stop":
                continue
            if str(o.get("status", "")).lower() not in ("open", "pending", "partially_filled"):
                continue
            tkr = o.get("symbol")
            if not tkr:
                continue
            out.setdefault(tkr, []).append(
                {
                    "order_id": str(o.get("id")),
                    "quantity": Decimal(str(o.get("quantity", 0))),
                    "stop_price": Decimal(str(o.get("stop_price", 0) or 0)),
                }
            )
        return out

    def cancel_order(self, order_id) -> dict:
        try:
            self._request("DELETE", f"/v1/accounts/{self.account_id}/orders/{order_id}", None)
            return {"status": "CANCELLED", "order_id": order_id}
        except Exception as e:
            return {"status": "error", "reason": str(e), "order_id": order_id}
