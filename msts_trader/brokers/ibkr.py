"""IBKR adapter — TWS / IB Gateway socket via ib_insync.

Built on the public `ib_insync` library
(https://ib-insync.readthedocs.io). Connects to a running TWS or IB
Gateway over the standard API socket. Default ports:

    TWS live      7496
    TWS paper     7497
    Gateway live  4001
    Gateway paper 4002

You must have TWS or IB Gateway running on your machine (or
reachable on your network — e.g. a Dockerised Gateway on
localhost:4002) and the API enabled (Configure → API → Enable ActiveX
and Socket Clients).

The IBKR API does not expose USD market value per position directly;
we derive it from `position.avgCost * position.position` and
overwrite with a fresh quote where available.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from ..models import Order, Position, Side
from .base import Balances, BrokerError

try:
    from ib_insync import IB, MarketOrder, Stock  # type: ignore
    _IB_OK = True
except ImportError:
    _IB_OK = False


class IBKR:
    name = "ibkr"
    supports_fractional = True  # IBKR supports US-stock fractional via OutsideRTH=False MKT orders on eligible symbols

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4002,
        client_id: int = 17,
        account_id: str | None = None,
        timeout: float = 10.0,
    ):
        if not _IB_OK:
            raise BrokerError("ib_insync not installed. Run: pip install ib_insync")
        self._ib = IB()
        try:
            self._ib.connect(host=str(host), port=int(port), clientId=int(client_id), timeout=float(timeout))
        except Exception as e:
            raise BrokerError(
                f"could not connect to IBKR at {host}:{port} (clientId={client_id}). "
                f"Is TWS / IB Gateway running with API enabled? — {e}"
            )

        accounts = self._ib.managedAccounts()
        if not accounts:
            self._ib.disconnect()
            raise BrokerError("IBKR session has no managed accounts; check login")
        if account_id and account_id in accounts:
            self.account_id = account_id
        else:
            if account_id:
                self._ib.disconnect()
                raise BrokerError(f"account {account_id!r} not in IBKR session (have: {accounts})")
            self.account_id = accounts[0]

    def __del__(self):
        try:
            ib = getattr(self, "_ib", None)
            if ib is not None and ib.isConnected():
                ib.disconnect()
        except Exception:
            pass

    # ----- Broker protocol -----

    def balances(self) -> Balances:
        rows = self._ib.accountSummary(self.account_id)
        m: dict[str, Decimal] = {}
        for r in rows:
            try:
                m[r.tag] = Decimal(str(r.value))
            except Exception:
                continue
        nav = m.get("NetLiquidation") or m.get("NetLiquidationByCurrency") or Decimal(0)
        cash = m.get("TotalCashValue") or m.get("CashBalance") or Decimal(0)
        bp = m.get("BuyingPower") or m.get("AvailableFunds") or Decimal(0)
        return Balances(nav=nav, cash=cash, buying_power=bp)

    def positions(self) -> dict[str, Position]:
        out: dict[str, Position] = {}
        for p in self._ib.positions(self.account_id):
            ct = p.contract
            if ct.secType != "STK":
                continue
            qty = Decimal(str(p.position))
            if qty == 0:
                continue
            avg = Decimal(str(p.avgCost or 0))
            out[ct.symbol] = Position(ticker=ct.symbol, quantity=qty, price=avg)
        return out

    def quote(self, tickers: Iterable[str]) -> dict[str, Decimal]:
        symbols = sorted({t.upper() for t in tickers})
        out: dict[str, Decimal] = {}
        for sym in symbols:
            try:
                ct = Stock(sym, "SMART", "USD")
                self._ib.qualifyContracts(ct)
                ticker = self._ib.reqMktData(ct, snapshot=True)
                self._ib.sleep(0.6)  # give the snapshot time to arrive
                px = (
                    _f(ticker.last) or _f(ticker.marketPrice()) or _f(ticker.close)
                    or _midpoint(ticker.bid, ticker.ask)
                )
                if px is not None and px > 0:
                    out[sym] = Decimal(str(px))
            except Exception:
                continue
            finally:
                try:
                    self._ib.cancelMktData(ct)
                except Exception:
                    pass
        return out

    def place_market(self, order: Order, dry_run: bool = False) -> dict:
        qty = float(round(float(order.quantity), 4))
        if qty <= 0:
            return {"status": "skipped", "reason": "qty<=0", "ticker": order.ticker}
        if dry_run:
            return {"status": "dry-run", "ticker": order.ticker, "side": order.side.value, "quantity": qty, "dry_run": True}

        ct = Stock(order.ticker, "SMART", "USD")
        self._ib.qualifyContracts(ct)
        action = "BUY" if order.side == Side.BUY else "SELL"
        mkt = MarketOrder(action, qty, account=self.account_id)
        try:
            trade = self._ib.placeOrder(ct, mkt)
        except Exception as e:
            return {"status": "error", "reason": str(e), "ticker": order.ticker}

        # Give IBKR ~2s to acknowledge; full fills usually arrive later.
        for _ in range(20):
            self._ib.sleep(0.1)
            if trade.orderStatus.status in {"Filled", "Submitted", "PreSubmitted", "ApiCancelled", "Cancelled"}:
                break
        return {
            "status": str(trade.orderStatus.status or "submitted"),
            "ticker": order.ticker,
            "side": order.side.value,
            "quantity": qty,
            "order_id": str(getattr(trade.order, "permId", "") or getattr(trade.order, "orderId", "")) or None,
            "dry_run": False,
        }


def _f(v) -> float | None:
    try:
        if v is None:
            return None
        # ib_insync returns nan for missing fields
        x = float(v)
        if x != x:  # NaN
            return None
        return x
    except Exception:
        return None


def _midpoint(bid, ask) -> float | None:
    b = _f(bid)
    a = _f(ask)
    if b is None or a is None or b <= 0 or a <= 0:
        return None
    return (b + a) / 2.0
