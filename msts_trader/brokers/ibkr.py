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
from .base import Balances, BrokerError, first_present

try:
    from ib_insync import IB, MarketOrder, Stock  # type: ignore
    from ib_insync import Order as IbOrder  # type: ignore
    _IB_OK = True
except ImportError:
    _IB_OK = False


class IBKR:
    name = "ibkr"
    supports_fractional = True  # IBKR supports US-stock fractional via OutsideRTH=False MKT orders on eligible symbols
    supports_moc = True  # orderType MOC — whole shares only

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
        # first_present, not `or`: a legitimate Decimal(0) must not fall through to the next tag
        nav = first_present(m.get("NetLiquidation"), m.get("NetLiquidationByCurrency"), Decimal(0))
        cash = first_present(m.get("TotalCashValue"), m.get("CashBalance"), Decimal(0))
        bp = first_present(m.get("BuyingPower"), m.get("AvailableFunds"), Decimal(0))
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
        """Batch snapshot quotes via reqTickers (blocks until data arrives).

        reqTickers is far more reliable than reqMktData + a fixed sleep: it
        waits for each snapshot to populate and cancels cleanly (no
        "Error 300: can't find EId" from double-cancellation). If the
        account lacks real-time data for a symbol, we retry once in
        delayed mode (reqMarketDataType 3) so quotes still come through.
        """
        symbols = sorted({t.upper() for t in tickers})
        if not symbols:
            return {}

        contracts = []
        for sym in symbols:
            try:
                ct = Stock(sym, "SMART", "USD")
                self._ib.qualifyContracts(ct)
                contracts.append((sym, ct))
            except Exception:
                continue

        out: dict[str, Decimal] = {}
        if not contracts:
            return out

        def _collect(market_data_type: int) -> list[str]:
            """Request tickers under a market-data type; return symbols still missing."""
            try:
                self._ib.reqMarketDataType(market_data_type)
            except Exception:
                pass
            missing: list[str] = []
            pending = [(s, c) for s, c in contracts if s not in out]
            try:
                tickers_ = self._ib.reqTickers(*[c for _, c in pending])
            except Exception:
                return [s for s, _ in pending]
            by_symbol = {t.contract.symbol: t for t in tickers_}
            for sym, _ in pending:
                t = by_symbol.get(sym)
                px = None
                if t is not None:
                    px = (
                        _f(t.last) or _f(t.close) or _f(t.marketPrice())
                        or _midpoint(t.bid, t.ask)
                    )
                if px is not None and px > 0:
                    out[sym] = Decimal(str(px))
                else:
                    missing.append(sym)
            return missing

        # 1) live data; 2) anything still missing → delayed (type 3).
        still_missing = _collect(1)
        if still_missing:
            _collect(3)
        return out

    def margin_requirement(self, orders) -> Decimal | None:
        """Real total initial-margin requirement for the BUY orders via
        whatIfOrder.initMarginChange. Returns None if any what-if fails or
        the account doesn't report margin (caller falls back to notional)."""
        total = Decimal(0)
        for o in orders:
            if o.side != Side.BUY:
                continue
            qty = float(round(float(o.quantity), 4))
            if qty <= 0:
                continue
            try:
                ct = Stock(o.ticker, "SMART", "USD")
                self._ib.qualifyContracts(ct)
                state = self._ib.whatIfOrder(ct, MarketOrder("BUY", qty, account=self.account_id))
            except Exception:
                return None
            chg = _f(getattr(state, "initMarginChange", None))
            if chg is None:
                return None
            total += Decimal(str(abs(chg)))
        return total

    def _build_order(self, action: str, qty: float, moc: bool):
        if moc:
            # Market-on-close: fills in the exchange closing auction.
            return IbOrder(action=action, totalQuantity=qty, orderType="MOC", tif="DAY", account=self.account_id)
        return MarketOrder(action, qty, account=self.account_id)

    def place_market(self, order: Order, dry_run: bool = False) -> dict:
        qty = float(round(float(order.quantity), 4))
        if order.moc:
            qty = float(int(qty))  # IBKR closing-auction orders are whole shares
        if qty <= 0:
            reason = "qty rounds to 0 (MOC requires whole shares)" if order.moc else "qty<=0"
            return {"status": "skipped", "reason": reason, "ticker": order.ticker}

        ct = Stock(order.ticker, "SMART", "USD")
        self._ib.qualifyContracts(ct)
        action = "BUY" if order.side == Side.BUY else "SELL"

        if dry_run:
            # Real broker-side validation via what-if: returns margin and
            # commission impact without ever transmitting the order.
            whatif = self._build_order(action, qty, order.moc)
            base = {"status": "dry-run", "ticker": order.ticker, "side": order.side.value, "quantity": qty, "moc": order.moc, "dry_run": True}
            try:
                state = self._ib.whatIfOrder(ct, whatif)
                base.update(
                    init_margin_change=_f(getattr(state, "initMarginChange", None)),
                    maint_margin_change=_f(getattr(state, "maintMarginChange", None)),
                    equity_with_loan_change=_f(getattr(state, "equityWithLoanChange", None)),
                    commission=_f(getattr(state, "commission", None)) or _f(getattr(state, "maxCommission", None)),
                    commission_currency=getattr(state, "commissionCurrency", None),
                )
            except Exception as e:
                base["note"] = f"what-if unavailable: {e}"
            return base

        mkt = self._build_order(action, qty, order.moc)
        try:
            trade = self._ib.placeOrder(ct, mkt)
        except Exception as e:
            return {"status": "error", "reason": str(e), "ticker": order.ticker}

        # Give IBKR ~2s to acknowledge; full fills usually arrive later.
        for _ in range(20):
            self._ib.sleep(0.1)
            if trade.orderStatus.status in {"Filled", "Submitted", "PreSubmitted", "ApiCancelled", "Cancelled"}:
                break

        status = str(trade.orderStatus.status or "submitted")
        result = {
            "status": status,
            "ticker": order.ticker,
            "side": order.side.value,
            "quantity": qty,
            "moc": order.moc,
            "order_id": str(getattr(trade.order, "permId", "") or getattr(trade.order, "orderId", "")) or None,
            "dry_run": False,
        }

        # Surface the real rejection reason from the trade log. IBKR cancels
        # with a terse status but the *why* (e.g. Error 201 KID/PRIIPs:
        # "this product does not have a KID" — EU retail can't trade US ETFs)
        # arrives as a log entry. Ignore the cosmetic 10349 TIF-preset note.
        reason = _reject_reason(trade)
        if reason and status in {"Cancelled", "ApiCancelled", "Inactive"}:
            result["status"] = "error"
            result["reason"] = reason
        return result


def _reject_reason(trade) -> str | None:
    """Extract the meaningful rejection message from an ib_insync trade log.

    Prefers a specific error (e.g. 201 KID/PRIIPs) over the generic 10349
    "TIF set to DAY based on order preset" — but if 10349 is the only thing
    present (as for a plain stock cancelled by an account order preset), we
    surface it rather than returning a bare "Cancelled" with no reason.
    """
    specific = None
    fallback = None
    for le in getattr(trade, "log", []) or []:
        code = getattr(le, "errorCode", 0) or 0
        if code == 0:
            continue
        msg = (getattr(le, "message", "") or "").replace("<br>", " ").strip()
        entry = f"IBKR {code}: {msg}"
        if code == 10349:
            fallback = entry + " (check TWS → Global Configuration → Presets)"
        else:
            specific = entry
    return specific or fallback


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
