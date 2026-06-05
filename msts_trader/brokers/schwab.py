"""Schwab adapter — OAuth2 via the public `schwab-py` SDK.

Built on https://github.com/alexgolec/schwab-py (MIT-licensed). Schwab's
Trader API uses OAuth2 with a 30-minute access token + a refresh token
that expires every 7 days, so subscribers must re-run the browser auth
flow weekly. Once a token file exists on disk, day-to-day rebalances
just read it.

One-time setup
--------------
1. Apply for a Schwab Developer account at
   https://developer.schwab.com (approval can take days).
2. Register an "Individual Developer" app. Set the callback URL to
   `https://127.0.0.1:8182/` (we'll spin up a local listener at login).
3. Copy the **app key** and **app secret** from the Schwab portal.
4. Run `msts-trader login --broker schwab` — a browser window opens,
   you authorize, and the resulting token JSON is stored in your OS
   keychain (the token file path lives in the keychain blob too).

The token JSON itself is written to
`~/.msts-trader/schwab_token.json` because schwab-py expects a file.
That file is gitignore-equivalent (~/.msts-trader is per-user).
"""
from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from ..models import Order, Position, Side
from .base import Balances, BrokerError

try:
    from schwab.auth import client_from_token_file, easy_client  # type: ignore
    from schwab.orders.equities import (  # type: ignore
        equity_buy_market,
        equity_sell_market,
    )
    _SCHWAB_OK = True
except ImportError:
    _SCHWAB_OK = False

TOKEN_PATH = Path(os.path.expanduser("~/.msts-trader/schwab_token.json"))


class Schwab:
    name = "schwab"
    supports_fractional = False  # Schwab Trader API places whole-share equity orders

    def __init__(self, app_key: str, app_secret: str, callback_url: str = "https://127.0.0.1:8182/", account_hash: str | None = None):
        if not _SCHWAB_OK:
            raise BrokerError("schwab-py not installed. Run: pip install schwab-py")
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        # enforce_enums=False so we can pass plain strings like fields=["positions"]
        # instead of schwab-py's Client.Account.Fields enums. Without this the
        # default (enforce_enums=True) rejects the string and balances()/
        # positions() fail on the first real call.
        if TOKEN_PATH.exists():
            self._client = client_from_token_file(str(TOKEN_PATH), app_key, app_secret, enforce_enums=False)
        else:
            # easy_client opens a browser and runs the local callback listener
            self._client = easy_client(
                api_key=app_key,
                app_secret=app_secret,
                callback_url=callback_url,
                token_path=str(TOKEN_PATH),
                enforce_enums=False,
            )
        self._account_hash = account_hash or self._discover_account_hash()
        self.account_id = self._account_hash[:8] + "…"  # display-safe truncation

    def _discover_account_hash(self) -> str:
        resp = self._client.get_account_numbers()
        resp.raise_for_status()
        accts = resp.json()
        if not accts:
            raise BrokerError("Schwab session has no accounts; check login")
        return accts[0]["hashValue"]

    # ----- Broker protocol -----

    def balances(self) -> Balances:
        resp = self._client.get_account(self._account_hash, fields=["positions"])
        resp.raise_for_status()
        a = resp.json().get("securitiesAccount", {})
        cur = a.get("currentBalances") or {}
        nav = Decimal(str(cur.get("liquidationValue") or cur.get("equity") or 0))
        cash = Decimal(str(cur.get("cashBalance") or 0))
        bp = Decimal(str(cur.get("buyingPower") or cur.get("dayTradingBuyingPower") or 0))
        return Balances(nav=nav, cash=cash, buying_power=bp)

    def positions(self) -> dict[str, Position]:
        resp = self._client.get_account(self._account_hash, fields=["positions"])
        resp.raise_for_status()
        a = resp.json().get("securitiesAccount", {})
        out: dict[str, Position] = {}
        for p in a.get("positions", []) or []:
            inst = p.get("instrument") or {}
            sym = inst.get("symbol")
            asset_type = inst.get("assetType") or inst.get("type")
            if not sym or asset_type not in ("EQUITY", "ETF", "COLLECTIVE_INVESTMENT"):
                continue
            qty = Decimal(str((p.get("longQuantity") or 0) - (p.get("shortQuantity") or 0)))
            if qty == 0:
                continue
            mv = Decimal(str(p.get("marketValue") or 0))
            price = mv / qty if qty else Decimal(0)
            out[sym] = Position(ticker=sym, quantity=qty, price=price)
        return out

    def quote(self, tickers: Iterable[str]) -> dict[str, Decimal]:
        symbols = sorted({t.upper() for t in tickers})
        if not symbols:
            return {}
        try:
            resp = self._client.get_quotes(symbols)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return {}
        out: dict[str, Decimal] = {}
        for sym, payload in data.items():
            q = (payload.get("quote") if isinstance(payload, dict) else None) or {}
            px = q.get("lastPrice") or q.get("mark") or q.get("closePrice")
            if px and float(px) > 0:
                out[sym] = Decimal(str(px))
        return out

    def place_market(self, order: Order, dry_run: bool = False) -> dict:
        qty = int(order.quantity)  # Schwab equity orders are whole shares
        if qty <= 0:
            return {"status": "skipped", "reason": "qty rounds to 0 (Schwab requires whole shares)", "ticker": order.ticker}
        if dry_run:
            return {"status": "dry-run", "ticker": order.ticker, "side": order.side.value, "quantity": qty, "dry_run": True}

        spec = (
            equity_buy_market(order.ticker, qty)
            if order.side == Side.BUY
            else equity_sell_market(order.ticker, qty)
        )
        try:
            resp = self._client.place_order(self._account_hash, spec.build())
            resp.raise_for_status()
        except Exception as e:
            return {"status": "error", "reason": str(e), "ticker": order.ticker}

        # Schwab returns 201 + Location header with the order id; body is empty on success
        location = resp.headers.get("Location") or ""
        order_id = location.rsplit("/", 1)[-1] if location else None
        return {
            "status": "submitted",
            "ticker": order.ticker,
            "side": order.side.value,
            "quantity": qty,
            "order_id": order_id,
            "dry_run": False,
        }


def has_token() -> bool:
    return TOKEN_PATH.exists()


def token_path() -> str:
    return str(TOKEN_PATH)


def clear_token() -> None:
    try:
        TOKEN_PATH.unlink()
    except FileNotFoundError:
        pass
