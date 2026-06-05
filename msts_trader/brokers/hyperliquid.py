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
