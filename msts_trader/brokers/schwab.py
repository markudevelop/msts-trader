"""Schwab adapter — OAuth2 via the public `schwab-py` SDK.

Built on https://github.com/alexgolec/schwab-py (MIT-licensed). Schwab's
Trader API uses OAuth2 with a 30-minute access token + a refresh token
that expires every 7 days, so subscribers must re-run the browser auth
flow weekly. Once a token exists in the OS keychain, day-to-day
rebalances just read it.

One-time setup
--------------
1. Apply for a Schwab Developer account at
   https://developer.schwab.com (approval can take days).
2. Register an "Individual Developer" app. Set the callback URL to
   `https://127.0.0.1:8182` (we'll spin up a local listener at login).
   Schwab matches the callback character-for-character — the URL used
   at login must EXACTLY equal the registered one, trailing slash
   included.
3. Copy the **app key** and **app secret** from the Schwab portal.
4. Run `msts-trader login --broker schwab` — a browser window opens,
   you authorize, and the resulting token JSON is stored in your OS
   keychain.

Older msts-trader releases wrote the token to
`~/.msts-trader/schwab_token.json` because schwab-py expects file-like
token persistence by default. This adapter now uses schwab-py's token
read/write callback API instead; any legacy token file is migrated into
the keychain and then removed on next Schwab use.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from .. import keychain
from ..models import Order, Position, Side
from .base import Balances, BrokerError, first_present

try:
    from schwab.auth import client_from_access_functions, client_from_login_flow  # type: ignore
    from schwab.orders.equities import (  # type: ignore
        equity_buy_limit,
        equity_buy_market,
        equity_sell_limit,
        equity_sell_market,
    )

    _SCHWAB_OK = True
except ImportError:
    _SCHWAB_OK = False

TOKEN_KEY = "schwab_oauth_token"
TOKEN_PATH = Path(os.path.expanduser("~/.msts-trader/schwab_token.json"))  # legacy plaintext cache
TOKEN_MAX_AGE_SECONDS = int(60 * 60 * 24 * 6.5)


class Schwab:
    name = "schwab"
    supports_fractional = False  # Schwab Trader API places whole-share equity orders
    supports_moc = True  # orderType MARKET_ON_CLOSE
    supports_stops = True  # GTC sell stop via schwab-py order spec
    supports_limit_chase = True  # LIMIT DAY via equity_buy_limit/equity_sell_limit

    # No trailing slash — schwab-py's recommended registration value. Schwab
    # rejects the OAuth redirect when this doesn't EXACTLY match the URL
    # registered on the app (trailing slash included), so the default here
    # must mirror what the setup instructions tell users to register.
    DEFAULT_CALLBACK_URL = "https://127.0.0.1:8182"

    def __init__(
        self, app_key: str, app_secret: str, callback_url: str = DEFAULT_CALLBACK_URL, account_hash: str | None = None
    ):
        if not _SCHWAB_OK:
            raise BrokerError("schwab-py not installed. Run: pip install schwab-py")
        # enforce_enums=False so we can pass plain strings like fields=["positions"]
        # instead of schwab-py's Client.Account.Fields enums. Without this the
        # default (enforce_enums=True) rejects the string and balances()/
        # positions() fail on the first real call.
        client = _client_from_stored_token(app_key, app_secret)
        if client is None:
            # client_from_login_flow opens a browser and runs the local callback
            # listener. token_path is retained only for schwab-py's messages;
            # token_write_func keeps the durable token in the OS keychain.
            self._client = client_from_login_flow(
                api_key=app_key,
                app_secret=app_secret,
                callback_url=callback_url,
                token_path=str(TOKEN_PATH),
                token_write_func=_write_token,
                enforce_enums=False,
            )
            _delete_legacy_token_file()
        else:
            self._client = client
        self._account_hash = account_hash or self._discover_account_hash()
        self.account_hash = self._account_hash  # stable public attr for login / creds reuse
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
        # first_present, not `or`: a legitimate 0 must not fall through to the next field
        nav = Decimal(str(first_present(cur.get("liquidationValue"), cur.get("equity"), 0)))
        cash = Decimal(str(first_present(cur.get("cashBalance"), 0)))
        bp = Decimal(str(first_present(cur.get("buyingPower"), cur.get("dayTradingBuyingPower"), 0)))
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
            return {
                "status": "skipped",
                "reason": "qty rounds to 0 (Schwab requires whole shares)",
                "ticker": order.ticker,
            }
        if dry_run:
            return {
                "status": "dry-run",
                "ticker": order.ticker,
                "side": order.side.value,
                "quantity": qty,
                "moc": order.moc,
                "dry_run": True,
            }

        spec = equity_buy_market(order.ticker, qty) if order.side == Side.BUY else equity_sell_market(order.ticker, qty)
        if order.moc:
            from schwab.orders.common import OrderType as SchwabOrderType  # type: ignore

            spec = spec.set_order_type(SchwabOrderType.MARKET_ON_CLOSE)
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
            "moc": order.moc,
            "order_id": order_id,
            "dry_run": False,
        }

    # ---- limit chase ------------------------------------------------------
    def place_limit(self, order: Order, limit_price: Decimal, dry_run: bool = False) -> dict:
        """LIMIT DAY order for the chase engine. Schwab equities are
        whole-share — dust is skipped and the engine market-fallbacks it."""
        qty = int(order.quantity)
        if qty <= 0:
            return {"status": "skipped", "reason": "qty rounds to 0 (Schwab whole shares)", "ticker": order.ticker}
        px = round(float(limit_price), 2)
        if dry_run:
            return {
                "status": "dry-run",
                "ticker": order.ticker,
                "side": order.side.value,
                "quantity": qty,
                "limit_price": px,
                "dry_run": True,
            }
        spec = (
            equity_buy_limit(order.ticker, qty, px)
            if order.side == Side.BUY
            else equity_sell_limit(order.ticker, qty, px)
        )
        try:
            resp = self._client.place_order(self._account_hash, spec.build())
            resp.raise_for_status()
        except Exception as e:
            return {"status": "error", "reason": str(e), "ticker": order.ticker}
        location = resp.headers.get("Location") or ""
        order_id = location.rsplit("/", 1)[-1] if location else None
        return {
            "status": "submitted",
            "ticker": order.ticker,
            "side": order.side.value,
            "quantity": qty,
            "order_id": order_id,
            "limit_price": px,
            "dry_run": False,
        }

    def order_status(self, order_id) -> dict:
        from ..chase import CANCELLED, FILLED, PARTIAL, REJECTED, UNKNOWN, WORKING

        try:
            resp = self._client.get_order(order_id, self._account_hash)
            resp.raise_for_status()
            o = resp.json()
        except Exception as e:
            return {"status": UNKNOWN, "filled_qty": 0.0, "filled_avg_price": None, "reason": str(e)}
        raw = str(o.get("status", "")).upper()
        filled = float(o.get("filledQuantity") or 0)
        # Weighted average from the execution legs (Schwab omits a top-level avg).
        tot_q = 0.0
        tot_c = 0.0
        for act in o.get("orderActivityCollection", []) or []:
            for leg in act.get("executionLegs", []) or []:
                q = float(leg.get("quantity") or 0)
                p = float(leg.get("price") or 0)
                tot_q += q
                tot_c += q * p
        avg = (tot_c / tot_q) if tot_q > 0 else None
        if raw == "FILLED":
            status = FILLED
        elif raw == "REJECTED":
            status = REJECTED
        elif raw in ("CANCELED", "CANCELLED", "EXPIRED", "REPLACED"):
            status = CANCELLED
        elif filled > 0:
            status = PARTIAL
        elif raw in (
            "WORKING",
            "QUEUED",
            "ACCEPTED",
            "PENDING_ACTIVATION",
            "NEW",
            "AWAITING_MANUAL_REVIEW",
            "PENDING_ACKNOWLEDGEMENT",
        ):
            status = WORKING
        else:
            status = UNKNOWN
        return {"status": status, "filled_qty": filled, "filled_avg_price": avg}

    # ---- protective stops -------------------------------------------------
    def place_stop(self, ticker: str, quantity, stop_price, dry_run: bool = False) -> dict:
        qty = int(quantity)
        if qty <= 0:
            return {"status": "skipped", "reason": "whole-share qty rounds to 0", "ticker": ticker}
        if dry_run:
            return {"status": "dry-run", "ticker": ticker, "stop_price": float(stop_price), "dry_run": True}
        from schwab.orders.common import Duration, OrderType as SOT  # type: ignore

        spec = (
            equity_sell_market(ticker, qty)
            .set_order_type(SOT.STOP)
            .set_stop_price(f"{float(stop_price):.2f}")
            .set_duration(Duration.GOOD_TILL_CANCEL)
        )
        try:
            resp = self._client.place_order(self._account_hash, spec.build())
            resp.raise_for_status()
            oid = (resp.headers.get("Location") or "").rstrip("/").split("/")[-1]
            return {
                "status": "submitted",
                "ticker": ticker,
                "order_id": oid,
                "quantity": qty,
                "stop_price": float(stop_price),
                "dry_run": False,
            }
        except Exception as e:
            return {"status": "error", "reason": str(e), "ticker": ticker}

    def open_stops(self) -> dict:
        out: dict = {}
        try:
            from schwab.client import Client  # type: ignore

            resp = self._client.get_orders_for_account(self._account_hash, status=Client.Order.Status.WORKING)
            resp.raise_for_status()
            orders = resp.json()
        except Exception:
            return out
        for o in orders or []:
            if str(o.get("orderType", "")).upper() != "STOP":
                continue
            for leg in o.get("orderLegCollection", []):
                sym = (leg.get("instrument") or {}).get("symbol")
                if not sym:
                    continue
                out.setdefault(sym, []).append(
                    {
                        "order_id": str(o.get("orderId")),
                        "quantity": Decimal(str(leg.get("quantity", 0))),
                        "stop_price": Decimal(str(o.get("stopPrice", 0) or 0)),
                    }
                )
        return out

    def cancel_order(self, order_id) -> dict:
        try:
            resp = self._client.cancel_order(order_id, self._account_hash)
            resp.raise_for_status()
            return {"status": "CANCELLED", "order_id": order_id}
        except Exception as e:
            return {"status": "error", "reason": str(e), "order_id": order_id}


def _read_token() -> dict | None:
    raw = keychain.load_secret(TOKEN_KEY)
    if not raw:
        return None
    try:
        token = json.loads(raw)
    except json.JSONDecodeError as e:
        raise BrokerError(
            "stored Schwab token is not valid JSON; run `msts-trader login --broker schwab --reauth`"
        ) from e
    if not isinstance(token, dict):
        raise BrokerError("stored Schwab token is invalid; run `msts-trader login --broker schwab --reauth`")
    return token


def _write_token(token: dict, *args, **kwargs) -> None:
    keychain.save_secret(TOKEN_KEY, json.dumps(token))
    _delete_legacy_token_file()


def _delete_legacy_token_file(*, strict: bool = False) -> None:
    try:
        TOKEN_PATH.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        if strict:
            raise BrokerError(
                f"stored Schwab token in the OS keychain but could not remove legacy plaintext file {TOKEN_PATH}; "
                "delete that file manually"
            ) from e
        # The keychain write already succeeded. Do not break an otherwise good
        # session if the old plaintext file cannot be removed right now.
        pass


def _migrate_legacy_token_file() -> dict | None:
    if not TOKEN_PATH.exists():
        return None
    try:
        raw = TOKEN_PATH.read_text(encoding="utf-8")
        token = json.loads(raw)
    except Exception as e:
        raise BrokerError(
            f"could not read legacy Schwab token file {TOKEN_PATH}; "
            "delete it or re-run `msts-trader login --broker schwab --reauth`"
        ) from e
    if not isinstance(token, dict):
        raise BrokerError(
            f"legacy Schwab token file {TOKEN_PATH} is invalid; "
            "delete it or re-run `msts-trader login --broker schwab --reauth`"
        )
    keychain.save_secret(TOKEN_KEY, json.dumps(token))
    _delete_legacy_token_file(strict=True)
    return token


def _load_stored_token() -> dict | None:
    return _read_token() or _migrate_legacy_token_file()


def _client_from_stored_token(app_key: str, app_secret: str):
    token = _load_stored_token()
    if token is None:
        return None
    client = client_from_access_functions(
        app_key,
        app_secret,
        lambda: token,
        _write_token,
        enforce_enums=False,
    )
    if TOKEN_MAX_AGE_SECONDS > 0 and client.token_age() >= TOKEN_MAX_AGE_SECONDS:
        clear_token()
        return None
    return client


def has_token() -> bool:
    return bool(keychain.load_secret(TOKEN_KEY)) or TOKEN_PATH.exists()


def token_location() -> str:
    if keychain.load_secret(TOKEN_KEY):
        return f"OS keychain ({keychain.SERVICE}:{keychain.SECRET_PREFIX}{TOKEN_KEY})"
    if TOKEN_PATH.exists():
        return str(TOKEN_PATH)
    return f"OS keychain ({keychain.SERVICE}:{keychain.SECRET_PREFIX}{TOKEN_KEY})"


def token_path() -> str:
    return str(TOKEN_PATH)


def clear_token() -> None:
    keychain.clear_secret(TOKEN_KEY)
    try:
        TOKEN_PATH.unlink()
    except FileNotFoundError:
        pass
