"""Hyperliquid adapter — crypto perpetuals DEX (EXPERIMENTAL).

Built on the public `hyperliquid-python-sdk` + `eth-account`. Install with
`pip install "msts-trader[hyperliquid]"`.

Caveats — read before live use:
  - Hyperliquid trades crypto PERPS, not equities. "weight" is fraction of
    account value; size = weight x NAV / mark price (rounded to the coin's
    size decimals). Leverage is set on the exchange side, not here.
  - Tickers are bare coin symbols: BTC, ETH, SOL (a "BTC-USD" style CSV is
    normalised to "BTC").
  - This adapter has NOT been verified end-to-end against a live account by
    the author. Test on testnet first (HL_TESTNET=1) and with tiny size.

Auth (env or creds-file):
  HL_PRIVATE_KEY      API-wallet / agent private key (hex)
  HL_ACCOUNT_ADDRESS  main account address (optional; defaults to the key's)
  HL_TESTNET          "1" to use the testnet endpoint
"""
from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from ..models import Order, Position, Side
from .base import Balances, BrokerError

try:
    import eth_account  # type: ignore
    from hyperliquid.exchange import Exchange  # type: ignore
    from hyperliquid.info import Info  # type: ignore
    from hyperliquid.utils import constants  # type: ignore
    _HL_OK = True
except ImportError:
    _HL_OK = False


def _coin(ticker: str) -> str:
    """Normalise SPY-style tickers to HL coin symbols: BTC-USD -> BTC."""
    t = ticker.upper().strip()
    for suffix in ("-USD", "-USDC", "-PERP", "/USD", "/USDC"):
        if t.endswith(suffix):
            return t[: -len(suffix)]
    return t


class Hyperliquid:
    name = "hyperliquid"
    supports_fractional = True
    supports_moc = False  # crypto perps trade 24/7 — no closing auction
    supports_stops = False  # perps use trigger orders; equity stop path never routes here
    supports_limit_chase = True  # GTC limit via exchange.order (EXPERIMENTAL — see module docstring)

    def __init__(self, private_key: str, account_address: str | None = None, testnet: bool = False):
        if not _HL_OK:
            raise BrokerError("hyperliquid deps not installed. Run: pip install \"msts-trader[hyperliquid]\"")
        if not private_key:
            raise BrokerError("private_key required")
        try:
            wallet = eth_account.Account.from_key(private_key)
        except Exception as e:
            raise BrokerError(f"invalid private key: {e}")
        self._address = account_address or wallet.address
        base_url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL
        self._info = Info(base_url, skip_ws=True)
        self._exchange = Exchange(wallet, base_url, account_address=self._address)
        self.account_id = self._address[:6] + "…" + self._address[-4:]
        self._meta = None  # lazy szDecimals lookup
        self._oid_coin: dict[str, str] = {}  # oid -> coin, so cancel_order can resolve the market

    # ----- Broker protocol -----

    def balances(self) -> Balances:
        st = self._info.user_state(self._address)
        ms = st.get("marginSummary", {}) if isinstance(st, dict) else {}
        nav = Decimal(str(ms.get("accountValue") or 0))
        withdrawable = Decimal(str(st.get("withdrawable") or 0)) if isinstance(st, dict) else Decimal(0)
        return Balances(nav=nav, cash=withdrawable, buying_power=withdrawable)

    def positions(self) -> dict[str, Position]:
        st = self._info.user_state(self._address)
        out: dict[str, Position] = {}
        for ap in (st.get("assetPositions") or []) if isinstance(st, dict) else []:
            pos = ap.get("position") or {}
            coin = pos.get("coin")
            szi = pos.get("szi")
            if not coin or szi in (None, "0", 0):
                continue
            qty = Decimal(str(szi))
            px = Decimal(str(pos.get("entryPx") or pos.get("markPx") or 0))
            out[coin] = Position(ticker=coin, quantity=qty, price=px)
        return out

    def quote(self, tickers: Iterable[str]) -> dict[str, Decimal]:
        coins = {_coin(t) for t in tickers}
        try:
            mids = self._info.all_mids()
        except Exception:
            return {}
        out: dict[str, Decimal] = {}
        for coin in coins:
            v = mids.get(coin)
            if v is not None:
                out[coin] = Decimal(str(v))
        return out

    def _sz_decimals(self, coin: str) -> int:
        if self._meta is None:
            try:
                self._meta = {a["name"]: a for a in self._info.meta().get("universe", [])}
            except Exception:
                self._meta = {}
        a = self._meta.get(coin) or {}
        return int(a.get("szDecimals", 4))

    def place_market(self, order: Order, dry_run: bool = False) -> dict:
        coin = _coin(order.ticker)
        # Order.quantity from the diff engine is shares; for HL it is coin size.
        sz = round(float(order.quantity), self._sz_decimals(coin))
        if sz <= 0:
            return {"status": "skipped", "reason": "size<=0", "ticker": coin}
        is_buy = order.side == Side.BUY
        if dry_run:
            return {"status": "dry-run", "ticker": coin, "side": order.side.value, "quantity": sz, "dry_run": True}
        try:
            resp = self._exchange.market_open(coin, is_buy, sz)
        except Exception as e:
            return {"status": "error", "reason": str(e), "ticker": coin}
        status = "submitted"
        order_id = None
        try:
            statuses = resp["response"]["data"]["statuses"]
            first = statuses[0]
            if "filled" in first:
                status = "FILLED"
                order_id = str(first["filled"].get("oid"))
            elif "resting" in first:
                status = "resting"
                order_id = str(first["resting"].get("oid"))
            elif "error" in first:
                return {"status": "error", "reason": first["error"], "ticker": coin}
        except Exception:
            pass
        return {"status": status, "ticker": coin, "side": order.side.value, "quantity": sz, "order_id": order_id, "dry_run": False}

    # ---- limit chase (EXPERIMENTAL) --------------------------------------
    @staticmethod
    def _round_px(px: float) -> float:
        """Best-effort price rounding to HL's ~5 significant figures. A price
        the exchange still rejects bubbles up as an error and the chase engine
        falls back to a market order."""
        from decimal import Decimal as D

        if px <= 0:
            return px
        d = D(str(px))
        quant = D(1).scaleb(d.adjusted() - 4)  # 5 significant figures
        return float(d.quantize(quant))

    def place_limit(self, order: Order, limit_price: Decimal, dry_run: bool = False) -> dict:
        coin = _coin(order.ticker)
        sz = round(float(order.quantity), self._sz_decimals(coin))
        if sz <= 0:
            return {"status": "skipped", "reason": "size<=0", "ticker": coin}
        px = self._round_px(float(limit_price))
        is_buy = order.side == Side.BUY
        if dry_run:
            return {"status": "dry-run", "ticker": coin, "side": order.side.value,
                    "quantity": sz, "limit_price": px, "dry_run": True}
        try:
            resp = self._exchange.order(coin, is_buy, sz, px, {"limit": {"tif": "Gtc"}})
        except Exception as e:
            return {"status": "error", "reason": str(e), "ticker": coin}
        status = "submitted"
        order_id = None
        try:
            first = resp["response"]["data"]["statuses"][0]
            if "filled" in first:
                status = "FILLED"
                order_id = str(first["filled"].get("oid"))
            elif "resting" in first:
                status = "submitted"
                order_id = str(first["resting"].get("oid"))
            elif "error" in first:
                return {"status": "error", "reason": first["error"], "ticker": coin}
        except Exception:
            pass
        if order_id:
            self._oid_coin[order_id] = coin
        return {"status": status, "ticker": coin, "side": order.side.value,
                "quantity": sz, "order_id": order_id, "limit_price": px, "dry_run": False}

    def order_status(self, order_id) -> dict:
        from ..chase import CANCELLED, FILLED, PARTIAL, REJECTED, UNKNOWN, WORKING

        try:
            res = self._info.query_order_by_oid(self._address, int(order_id))
        except Exception as e:
            return {"status": UNKNOWN, "filled_qty": 0.0, "filled_avg_price": None, "reason": str(e)}
        wrap = res.get("order") if isinstance(res, dict) else None
        if not wrap:
            return {"status": UNKNOWN, "filled_qty": 0.0, "filled_avg_price": None}
        inner = str(wrap.get("status", "")).lower()
        od = wrap.get("order") or {}
        try:
            orig = float(od.get("origSz") or 0)
            rem = float(od.get("sz") or 0)
        except Exception:
            orig, rem = 0.0, 0.0
        filled = max(0.0, orig - rem)
        if inner == "filled":
            status = FILLED
        elif inner in ("open", "triggered"):
            status = PARTIAL if filled > 0 else WORKING
        elif "reject" in inner:
            status = REJECTED
        elif "cancel" in inner:
            status = CANCELLED
        else:
            status = UNKNOWN
        # This endpoint doesn't expose an average fill price; leave it None
        # (the chase engine treats fill_price as optional).
        return {"status": status, "filled_qty": filled, "filled_avg_price": None}

    def cancel_order(self, order_id) -> dict:
        coin = self._oid_coin.get(str(order_id))
        if coin is None:
            try:
                for oo in self._info.open_orders(self._address):
                    if str(oo.get("oid")) == str(order_id):
                        coin = oo.get("coin")
                        break
            except Exception:
                pass
        if coin is None:
            return {"status": "error", "reason": "cannot resolve coin for oid", "order_id": order_id}
        try:
            self._exchange.cancel(coin, int(order_id))
            self._oid_coin.pop(str(order_id), None)
            return {"status": "CANCELLED", "order_id": order_id}
        except Exception as e:
            return {"status": "error", "reason": str(e), "order_id": order_id}
