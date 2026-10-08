"""Local web UI server (`msts-trader ui`).

Security model — this process can place real orders, so:
  - binds 127.0.0.1 only;
  - every /api request must carry the per-process random token
    (X-MSTS-Token), which is only ever handed out in the URL printed/opened
    at startup. A malicious web page can make the browser POST to localhost
    but cannot read or guess the token (CSRF / DNS-rebinding defence);
  - a request with an Origin header must come from this server's own origin;
  - live execution additionally requires the strategy's `live_enabled` flag
    AND a typed confirmation (the strategy id) on every run.
"""

from __future__ import annotations

import os
import re
import secrets
import subprocess
import sys
import threading
import webbrowser
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..brokers import EXTENDED_SUPPORTED, MOC_SUPPORTED, SUPPORTED
from ..market_hours import market_status
from ..symphony import backtest, composer_import, feeds, performance, prices, runner, store, studio_meta
from ..symphony.evaluate import EvalError
from ..symphony.model import (
    CHASE_LEAD_MINUTES,
    EXTENDED_LEAD_MINUTES,
    INDICATORS,
    MOC_LEAD_MINUTES,
    Feed,
    Symphony,
    combine,
    slugify,
    tickers,
)

STATIC_DIR = Path(__file__).parent / "static"
TOKEN_HEADER = "x-msts-token"


def _bad(status: int, msg: str):
    raise HTTPException(status_code=status, detail=msg)


def _sym_or_422(data: dict) -> Symphony:
    try:
        return Symphony.model_validate(data)
    except Exception as e:
        _bad(422, f"invalid strategy: {e}")


def _get(sid: str) -> Symphony:
    try:
        return store.get(sid)
    except store.StoreError as e:
        _bad(404, str(e))


def _summary(s: Symphony) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "description": s.description,
        "rebalance": s.rebalance,
        "tickers": tickers(s),
        "deploy": s.deploy.model_dump(),
    }


# Sleeve-tool inputs reach the CLI as positional args, so they must never
# start with "-" (it would parse as an option).
_SLEEVE_TICKER = re.compile(r"[A-Z0-9][A-Z0-9.\-^=/]{0,19}")
_SLEEVE_AMOUNT = re.compile(r"\$?\d+(\.\d+)?%?")


def _sleeve_state(sid: str, broker: str) -> list[dict]:
    return performance.sleeve_ledgers(sid, broker)


def _cli(args: list[str], timeout: int = 120) -> dict:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "msts_trader", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        env=env,
    )
    text = (proc.stdout + "\n" + proc.stderr).strip()
    return {"ok": proc.returncode == 0, "output": text[-2000:]}


def create_app(token: str, *, allowed_origins: set[str] | None = None, static_dir: Path = STATIC_DIR) -> FastAPI:
    app = FastAPI(title="msts-trader studio", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    allowed_origins = set(allowed_origins or ())
    dev_origin = os.environ.get("MSTS_UI_DEV_ORIGIN")
    if dev_origin:
        allowed_origins.add(dev_origin)
    run_lock = threading.Lock()

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            if origin and origin not in allowed_origins:
                return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
            got = request.headers.get(TOKEN_HEADER, "")
            if not secrets.compare_digest(got, token):
                return JSONResponse(
                    {"detail": "missing or bad session token — reopen the URL printed by `msts-trader ui`"},
                    status_code=401,
                )  # noqa: E501
        return await call_next(request)

    # ── meta ──────────────────────────────────────────────────────────
    @app.get("/api/meta")
    def meta():
        ms = market_status()
        return {
            "version": __version__,
            "brokers": list(SUPPORTED),
            "moc_brokers": list(MOC_SUPPORTED),
            "moc_lead_minutes": MOC_LEAD_MINUTES,
            "chase_lead_minutes": CHASE_LEAD_MINUTES,
            "extended_brokers": list(EXTENDED_SUPPORTED),
            "extended_lead_minutes": EXTENDED_LEAD_MINUTES,
            "indicators": list(INDICATORS),
            "market": {"status": ms.status, "minutes_to_close": ms.minutes_to_close},
        }

    # ── strategies CRUD ───────────────────────────────────────────────
    @app.get("/api/strategies")
    def list_strategies():
        return [_summary(s) for s in store.list_all()]

    @app.post("/api/strategies")
    def create_strategy(data: dict = Body(...)):
        data = dict(data)
        data["id"] = store.unique_id(data.get("id") or slugify(data.get("name") or "strategy"))
        s = _sym_or_422(data)
        store.save(s)
        return s.model_dump(by_alias=True)

    @app.get("/api/strategies/{sid}")
    def get_strategy(sid: str):
        s = _get(sid)
        studio_meta.mark_viewed(sid)
        return s.model_dump(by_alias=True)

    @app.put("/api/strategies/{sid}")
    def put_strategy(sid: str, data: dict = Body(...)):
        s = _sym_or_422(data)
        if s.id != sid:
            _bad(422, "id in body does not match the URL")
        if not store.exists(sid):
            _bad(404, f"no strategy {sid!r}")
        store.save(s)
        return s.model_dump(by_alias=True)

    @app.delete("/api/strategies/{sid}")
    def delete_strategy(sid: str):
        try:
            store.delete(sid)
        except store.StoreError as e:
            _bad(404, str(e))
        studio_meta.forget(sid)
        return {"deleted": sid}

    # ── evaluate / backtest (work on unsaved drafts too) ──────────────
    @app.post("/api/eval")
    def eval_draft(data: dict = Body(...)):
        s = _sym_or_422(data)
        try:
            return runner.current_weights(s)
        except (EvalError, prices.PriceError) as e:
            _bad(422, str(e))

    @app.post("/api/backtest")
    def backtest_draft(
        strategy: dict = Body(...),
        start: str | None = Body(None),
        end: str | None = Body(None),
        cost_bps: float = Body(5.0),
        benchmark: str = Body("SPY"),
    ):
        s = _sym_or_422(strategy)
        try:
            d0 = date.fromisoformat(start) if start else None
            d1 = date.fromisoformat(end) if end else None
        except ValueError as e:
            _bad(422, f"bad date: {e}")
        bm = (benchmark or "").strip().upper() or None
        try:
            closes = prices.load_closes(sorted(set(tickers(s)) | ({bm} if bm else set())))
            res = backtest.run(s, closes, start=d0, end=d1, cost_bps=cost_bps, benchmark=bm)
        except (EvalError, prices.PriceError) as e:
            _bad(422, str(e))
        # In-sample vs out-of-sample split: everything after go-live is OOS.
        live = performance.go_live(s.id, s.deploy.broker) if store.exists(s.id) else None
        res["oos_start"] = live.isoformat() if live else None
        if store.exists(s.id) and not start and not end:
            studio_meta.record_backtest(s.id, res)  # full-history runs feed the dashboard
        return res

    @app.post("/api/compare")
    def compare(
        strategies: list[dict] = Body(...),
        start: str | None = Body(None),
        end: str | None = Body(None),
        cost_bps: float = Body(5.0),
        benchmark: str = Body("SPY"),
    ):
        syms = [_sym_or_422(x) for x in strategies]
        if len(syms) < 2 or len(syms) > 8:
            _bad(422, "compare 2 to 8 strategies")
        if len({x.id for x in syms}) != len(syms):
            _bad(422, "each strategy can appear once")
        try:
            d0 = date.fromisoformat(start) if start else None
            d1 = date.fromisoformat(end) if end else None
        except ValueError as e:
            _bad(422, f"bad date: {e}")
        bm = (benchmark or "").strip().upper() or None
        universe = set().union(*(tickers(x) for x in syms)) | ({bm} if bm else set())
        try:
            closes = prices.load_closes(sorted(universe))
            return backtest.compare(syms, closes, start=d0, end=d1, cost_bps=cost_bps, benchmark=bm)
        except (EvalError, prices.PriceError) as e:
            _bad(422, str(e))

    # ── pnlportfolio books ──────────────────────────────────────────
    @app.get("/api/feeds")
    def feed_catalog():
        """Public book list + whether a token is stored (never the token itself)."""
        try:
            books = feeds.catalog()
        except feeds.FeedError as e:
            _bad(502, str(e))
        return {"provider": "pnlportfolio", "has_token": bool(feeds.get_token()), "books": books}

    @app.put("/api/feeds/token")
    def feed_token(token: str = Body(..., embed=True)):
        """Validate against a live book, then keep it in the OS keychain."""
        token = token.strip()
        if not token:
            _bad(422, "empty token")
        try:
            feeds.book_weights("unified", token=token)
        except feeds.FeedError as e:
            if "(401)" in str(e):
                _bad(422, str(e))
            # stale/unreachable: the token itself was accepted or can't be judged — keep it
        feeds.set_token(token)
        return {"has_token": True}

    @app.delete("/api/feeds/token")
    def feed_token_clear():
        feeds.clear_token()
        return {"has_token": bool(feeds.get_token())}

    @app.post("/api/feeds/import")
    def feed_import(books: list[str] = Body(..., embed=True)):
        """One strategy per book, each a single feed block (edit/blend freely after)."""
        if not books or len(books) > 20:
            _bad(422, "pick 1 to 20 books")
        try:
            meta = {b["id"]: b for b in feeds.catalog()}
        except feeds.FeedError:
            meta = {}
        created = []
        for book in books:
            label = (meta.get(book) or {}).get("label") or book
            try:
                s = Symphony.model_validate(
                    {
                        "id": store.unique_id(slugify(f"pnl-{book}")),
                        "name": f"{label} (pnlportfolio)",
                        "description": f"Holds the published pnlportfolio.com '{label}' book; backtests on its published NAV.",
                        "rebalance": "daily",
                        "children": [{"step": "feed", "book": book, "name": label}],
                    }
                )
            except Exception as e:
                _bad(422, f"bad book {book!r}: {e}")
            store.save(s)
            created.append(s.model_dump(by_alias=True))
        return {"created": created}

    # ── custom URL feeds ────────────────────────────────────────────
    def _url_feed(name, weights_url, nav_url, auth, token_param) -> Feed:
        try:
            return Feed.model_validate(
                {
                    "step": "feed",
                    "provider": "url",
                    "name": (name or "").strip() or None,
                    "weights_url": weights_url,
                    "nav_url": nav_url or None,
                    "auth": auth,
                    "token_param": token_param or "token",
                }
            )
        except Exception as e:
            _bad(422, f"invalid feed: {e}")

    @app.post("/api/feeds/url/test")
    def url_feed_test(
        weights_url: str = Body(...),
        nav_url: str | None = Body(None),
        auth: str = Body("none"),
        token_param: str = Body("token"),
        token: str | None = Body(None),
    ):
        """Fetch + parse without saving anything. Uses the typed token, else a
        stored one for this URL. Returns a summary only — never the token."""
        node = _url_feed("test", weights_url, nav_url, auth, token_param)
        tok = (token or "").strip() or feeds.get_feed_token(node.ref)
        try:
            w = feeds.url_weights(node, token=tok)
        except feeds.FeedError as e:
            _bad(422, str(e))
        top = sorted(w["weights"].items(), key=lambda kv: -kv[1])[:6]
        out = {
            "positions": len(w["weights"]),
            "gross": round(sum(w["weights"].values()), 6),
            "top": [[t, round(v, 6)] for t, v in top],
            "asof": w["date"],
            "stops": len(w["stops"]),
            "history": None,
            "token_stored": bool(feeds.get_feed_token(node.ref)),
        }
        if node.nav_url:
            try:
                h = feeds.url_history(node, token=tok)
                out["history"] = {
                    "start": h.index[0].date().isoformat(),
                    "end": h.index[-1].date().isoformat(),
                    "days": len(h),
                }
            except feeds.FeedError as e:
                out["history_error"] = str(e)
        return out

    @app.post("/api/feeds/url/import")
    def url_feed_import(
        name: str = Body(...),
        weights_url: str = Body(...),
        nav_url: str | None = Body(None),
        auth: str = Body("none"),
        token_param: str = Body("token"),
        token: str | None = Body(None),
    ):
        """Create a strategy holding one URL feed. A typed token goes to the OS
        keychain (keyed by the feed's URL hash), never into the strategy file."""
        if not name.strip():
            _bad(422, "give the feed a name")
        node = _url_feed(name, weights_url, nav_url, auth, token_param)
        tok = (token or "").strip()
        if node.auth != "none" and not tok and not feeds.get_feed_token(node.ref):
            _bad(422, "this feed sends a token: paste it once so it can be stored in your keychain")
        if tok:
            feeds.set_feed_token(node.ref, tok)
        s = Symphony.model_validate(
            {
                "id": store.unique_id(slugify(name)),
                "name": name.strip(),
                "description": "Holds the weights published at a custom URL"
                + ("; backtests on its history URL." if node.nav_url else "; history is recorded as Studio runs it."),
                "rebalance": "daily",
                "children": [node.model_dump(by_alias=True, exclude_none=True)],
            }
        )
        store.save(s)
        return s.model_dump(by_alias=True)

    @app.post("/api/combine")
    def combine_strategies(
        strategies: list[dict] = Body(...),
        weights: list[float] = Body(...),
        name: str | None = Body(None),
        rebalance: str | None = Body(None),
    ):
        """Build (not save) a blended strategy from 2-8 strategies."""
        syms = [_sym_or_422(x) for x in strategies]
        if not 2 <= len(syms) <= 8 or len(weights) != len(syms):
            _bad(422, "combine 2 to 8 strategies, one weight each")
        if rebalance is not None and rebalance not in ("daily", "weekly", "monthly", "quarterly", "yearly"):
            _bad(422, "bad rebalance cadence")
        label = (name or "").strip() or " + ".join(x.name for x in syms)[:80]
        try:
            blend = combine(list(zip(syms, weights)), id="__blend__", name=label, rebalance=rebalance)
        except ValueError as e:
            _bad(422, str(e))
        return blend.model_dump(by_alias=True)

    @app.get("/api/strategies/{sid}/performance")
    def strategy_performance(sid: str, benchmark: str = "SPY"):
        s = _get(sid)
        bm = (benchmark or "").strip().upper() or None
        held = set()
        for snap in store.read_snapshots(s.id):
            held |= set((snap.get("holdings") or {}).keys())
        live = performance.go_live(s.id, s.deploy.broker)
        if live is None:
            return performance.oos(s, None)  # -> {"live_since": None, ...}
        try:
            closes = prices.load_closes(sorted(set(tickers(s)) | held | ({bm} if bm else set())))
            return performance.oos(s, closes, benchmark=bm)
        except (EvalError, prices.PriceError, ValueError) as e:
            _bad(422, str(e))

    @app.post("/api/import")
    def import_symphony(text: str = Body(..., embed=True)):
        try:
            s, warnings = composer_import.import_text(text)
        except composer_import.ComposerImportError as e:
            _bad(422, str(e))
        s = s.model_copy(update={"id": store.unique_id(s.id)})
        store.save(s)
        return {"strategy": s.model_dump(by_alias=True), "warnings": warnings}

    # ── deploy: run / capital / sleeve / history ──────────────────────
    @app.post("/api/strategies/{sid}/run")
    def run_strategy(sid: str, mode: str = Body("dry"), confirm: str | None = Body(None), force: bool = Body(False)):
        s = _get(sid)
        if mode not in (runner.DRY, runner.LIVE):
            _bad(422, "mode must be 'dry' or 'live'")
        if mode == runner.LIVE:
            if not s.deploy.live_enabled:
                _bad(403, "live trading is off for this strategy — enable it in Deploy first")
            if confirm != s.id:
                _bad(403, f"type the strategy id ({s.id}) to confirm a live run")
        if not run_lock.acquire(blocking=False):
            _bad(409, "another run is in progress")
        try:
            return runner.run(s, mode=mode, force=force, source="ui")
        finally:
            run_lock.release()

    @app.post("/api/strategies/{sid}/capital")
    def capital(sid: str, action: str = Body(...), amount: str = Body(...)):
        s = _get(sid)
        if action not in ("invest", "divest"):
            _bad(422, "action must be invest or divest")
        try:
            if Decimal(amount) <= 0:
                raise InvalidOperation
        except InvalidOperation:
            _bad(422, "amount must be a positive number")
        args = ["sleeve", action, s.id, amount, "--broker", s.deploy.broker]
        if s.deploy.account:
            args += ["--account", s.deploy.account]
        res = _cli(args)
        if not res["ok"]:
            _bad(422, res["output"] or f"sleeve {action} failed")
        performance.snapshot(s, event=action)
        return {**res, "sleeve": _sleeve_state(s.id, s.deploy.broker)}

    # `msts-trader sleeve <action>` for this strategy's sleeve. None of these
    # trade: they edit the local ledger (adopt / release / adjust / base / cap)
    # or settle and compare it to the account (reconcile, account-wide).
    @app.post("/api/strategies/{sid}/sleeve-tool")
    def sleeve_tool(
        sid: str,
        action: str = Body(...),
        ticker: str | None = Body(None),
        qty: str | None = Body(None),
        spec: str | None = Body(None),
    ):
        s = _get(sid)
        if action in ("adopt", "release", "adjust"):
            tkr = (ticker or "").strip().upper()
            if not _SLEEVE_TICKER.fullmatch(tkr):
                _bad(422, "ticker must be a symbol like SPY or BRK.B")
            try:
                q = Decimal((qty or "").strip())
                if not q.is_finite() or q < 0 or (q == 0 and action != "adjust"):
                    raise InvalidOperation
            except InvalidOperation:
                _bad(422, "quantity must be a positive number of shares (0 is allowed only for set tally)")
            args = ["sleeve", action, s.id, tkr, str(q)]
        elif action in ("base", "cap"):
            sp = (spec or "").strip().lower().replace(",", "")
            allowed = ("own-nav",) if action == "base" else ("off",)
            if sp not in allowed and not _SLEEVE_AMOUNT.fullmatch(sp):
                _bad(422, f"{action} must be {allowed[0]}, a dollar amount like $50000, or a percent like 20%")
            args = ["sleeve", action, s.id, sp]
        elif action == "reconcile":
            args = ["sleeve", "reconcile"]
        else:
            _bad(422, "action must be adopt, release, adjust, base, cap or reconcile")
        args += ["--broker", s.deploy.broker]
        if s.deploy.account:
            args += ["--account", s.deploy.account]
        res = _cli(args)
        if res["ok"] and action in ("adopt", "release", "adjust"):
            performance.snapshot(s, event=action)
        # A failed reconcile still printed its table (the negative residuals
        # are the point), so failures come back as data, not an HTTP error.
        return {**res, "sleeve": _sleeve_state(s.id, s.deploy.broker)}

    @app.get("/api/strategies/{sid}/sleeve")
    def sleeve_state(sid: str):
        s = _get(sid)
        return {"broker": s.deploy.broker, "ledgers": _sleeve_state(s.id, s.deploy.broker)}

    @app.get("/api/runs")
    def runs(strategy: str | None = None, limit: int = 50):
        return store.read_runs(strategy, limit=min(max(limit, 1), 500))

    # ── dashboard / library ─────────────────────────────────────────
    @app.get("/api/dashboard")
    def dashboard():
        """One row per strategy (capital, held vs target positions, last
        rebalance check, last backtest, tags, recency) plus a combined-portfolio
        roll-up of the funded ones. Offline: values use cached closes only."""
        meta = studio_meta.all_meta()
        last_run: dict = {}
        last_target: dict = {}
        for e in reversed(store.read_runs(limit=100000)):  # oldest -> newest
            sid = e.get("strategy")
            last_run[sid] = e
            if e.get("weights") or e.get("target") == "cash":  # going to cash = an all-cash target
                last_target[sid] = e
        strategies = store.list_all()
        ledgers = {}
        for s in strategies:
            ledgers[s.id] = performance._pick(performance.sleeve_ledgers(s.id, s.deploy.broker), s.deploy.account)
        held_tickers = {t for led in ledgers.values() if led for t in led.get("holdings") or {}}
        px = prices.cached_last_close(held_tickers) if held_tickers else {}

        rows = []
        for s in strategies:
            led = ledgers[s.id] or {}
            m = meta.get(s.id, {})
            lr = last_run.get(s.id)
            lt = last_target.get(s.id)
            holdings = {t: float(q) for t, q in (led.get("holdings") or {}).items()}
            cash = float(led["cash"]) if led.get("cash") not in (None, "") else None
            unpriced = sorted(t for t in holdings if t not in px)
            priced = not unpriced
            nav = (cash or 0.0) + sum(q * px[t] for t, q in holdings.items()) if priced else None
            weights = (lt or {}).get("weights") or {}
            # Why targets aren't held, as the rebalance engine reported it on
            # the latest target run (rounds to 0 at whole shares, no quote, ...).
            # Runs from before 0.43.1 don't carry it -> None, not a guess.
            nb = ((lt or {}).get("preview") or {}).get("not_bought")
            not_bought = None if nb is None else [x for x in nb if x.get("ticker") not in holdings]
            sizing = ((lt or {}).get("preview") or {}).get("sizing")
            rows.append(
                {
                    "id": s.id,
                    "name": s.name,
                    "tags": s.tags,
                    "rebalance": s.rebalance,
                    "deploy": s.deploy.model_dump(),
                    "funded": bool(led.get("contributed") or led.get("cash") or led.get("holdings")),
                    # Home tabs: real money at a broker vs paper / preview-only (incubation)
                    "stage": "live" if s.deploy.real_money else "incubation",
                    "contributed": led.get("contributed"),
                    "cash": led.get("cash"),
                    "nav": None if nav is None else round(nav, 2),
                    # Held tickers with no cached close: Value can't be computed (cash
                    # alone would be misleading, e.g. negative for a levered book).
                    "unpriced": unpriced,
                    "positions": len(holdings),
                    "target_positions": len(lt.get("weights") or {}) if lt else None,
                    "not_bought": not_bought,
                    "sizing": sizing,
                    "live": performance.live_stats(s.id, s.deploy.broker),
                    "last_viewed": m.get("last_viewed"),
                    "last_backtest": m.get("last_backtest"),
                    "last_run": (
                        {
                            **{k: lr.get(k) for k in ("ts", "status", "mode", "source", "error", "target")},
                            "orders": len((lr.get("preview") or {}).get("orders") or []),
                        }
                        if lr
                        else None
                    ),
                    "_holdings": holdings,
                    "_weights": weights,
                    "_target_ts": (lt or {}).get("ts"),
                }
            )
        # One combined portfolio per stage: paper must never inflate the real-money totals.
        rollups = {
            st: _rollup([r for r in rows if r["funded"] and r["stage"] == st], px) for st in ("live", "incubation")
        }
        for r in rows:
            for k in ("_holdings", "_weights", "_target_ts"):
                r.pop(k)
        return {"strategies": rows, "rollups": rollups, "settings": studio_meta.get_settings()}

    def _rollup(funded: list[dict], px: dict) -> dict:
        """Combined portfolio of the funded strategies, per ticker:
        target $ = sum(latest target weight x strategy capital) and held $ =
        shares the sleeves own x cached close. Capital = sleeve NAV (cash +
        holdings), else contributed when a holding has no cached price."""
        by: dict = {}
        total_capital = 0.0
        unallocated = 0.0
        for r in funded:
            capital = r["nav"] if r["nav"] is not None else float(r["contributed"] or 0)
            total_capital += capital
            w = r["_weights"]
            unallocated += capital * max(0.0, 1.0 - sum(w.values()))
            for t, wt in w.items():
                e = by.setdefault(t, {"ticker": t, "target_value": 0.0, "held_qty": 0.0, "held_value": 0.0, "by": []})
                e["target_value"] += wt * capital
                e["by"].append({"id": r["id"], "name": r["name"], "weight": wt, "value": round(wt * capital, 2)})
            for t, q in r["_holdings"].items():
                e = by.setdefault(t, {"ticker": t, "target_value": 0.0, "held_qty": 0.0, "held_value": 0.0, "by": []})
                e["held_qty"] += q
                if t in px:
                    e["held_value"] += q * px[t]
        total_target = sum(e["target_value"] for e in by.values())
        total_held = sum(e["held_value"] for e in by.values())
        tickers = []
        for e in sorted(by.values(), key=lambda x: (-x["target_value"], -x["held_value"], x["ticker"])):
            tickers.append(
                {
                    **e,
                    "target_value": round(e["target_value"], 2),
                    "target_weight": (e["target_value"] / total_capital) if total_capital else None,
                    "held_qty": round(e["held_qty"], 6),
                    "held_value": round(e["held_value"], 2),
                    "priced": e["ticker"] in px or e["held_qty"] == 0,
                }
            )
        return {
            "strategies": len(funded),
            "with_targets": sum(1 for r in funded if r["_target_ts"]),
            "total_capital": round(total_capital, 2),
            "total_target": round(total_target, 2),
            "unallocated": round(unallocated, 2),
            "total_held": round(total_held, 2),
            "tickers": tickers,
        }

    @app.post("/api/strategies/bulk")
    def bulk(ids: list[str] = Body(...), action: str = Body(...), tag: str | None = Body(None)):
        if action not in ("tag", "untag", "delete", "pause", "resume"):
            _bad(422, "action must be tag, untag, delete, pause or resume")
        if action in ("tag", "untag") and not (tag or "").strip():
            _bad(422, "give a tag")
        done, failed = [], []
        for sid in dict.fromkeys(ids):
            try:
                s = store.get(sid)
                if action == "delete":
                    store.delete(sid)
                    studio_meta.forget(sid)
                else:
                    if action == "tag":
                        s = s.model_copy(update={"tags": [*s.tags, tag]})
                        s = Symphony.model_validate(s.model_dump(by_alias=True))
                    elif action == "untag":
                        s = s.model_copy(update={"tags": [t for t in s.tags if t.lower() != tag.strip().lower()]})
                    elif action in ("pause", "resume"):
                        s = s.model_copy(
                            update={"deploy": s.deploy.model_copy(update={"schedule_enabled": action == "resume"})}
                        )
                    store.save(s)
                done.append(sid)
            except Exception as e:
                failed.append({"id": sid, "error": str(e)})
        return {"done": done, "failed": failed}

    @app.post("/api/strategies/export")
    def export(ids: list[str] = Body(..., embed=True)):
        import io
        import zipfile

        from fastapi.responses import Response

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for sid in dict.fromkeys(ids):
                try:
                    z.writestr(f"{sid}.json", store.get(sid).model_dump_json(by_alias=True, indent=2))
                except store.StoreError:
                    continue
        return Response(
            buf.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="msts-strategies.zip"'},
        )

    @app.post("/api/strategies/cash")
    def go_to_cash(ids: list[str] = Body(...), confirm: str = Body(...), mode: str = Body("live")):
        """Sell everything each strategy's sleeve holds and pause its schedule.
        Real orders on real brokers: needs the typed confirmation CASH."""
        if confirm != "CASH":
            _bad(403, "type CASH to confirm")
        if mode not in (runner.DRY, runner.LIVE):
            _bad(422, "mode must be dry or live")
        if not run_lock.acquire(blocking=False):
            _bad(409, "another run is in progress")
        out = []
        try:
            for sid in dict.fromkeys(ids):
                try:
                    s = store.get(sid)
                except store.StoreError as e:
                    out.append({"strategy": sid, "status": "error", "error": str(e)})
                    continue
                if mode == runner.LIVE:
                    s = s.model_copy(update={"deploy": s.deploy.model_copy(update={"schedule_enabled": False})})
                    store.save(s)
                out.append(runner.run(s, mode=mode, source="ui", target={}))
        finally:
            run_lock.release()
        return {"results": out}

    # ── settings: notifications ──────────────────────────────────────
    @app.get("/api/settings")
    def settings_get():
        return studio_meta.get_settings()

    @app.put("/api/settings")
    def settings_put(
        notify_on: str | None = Body(None),
        telegram_chat_id: str | None = Body(None),
        weekly_digest: bool | None = Body(None),
        notify_url: str | None = Body(None),
        telegram_token: str | None = Body(None),
        home_tabs: list[str] | None = Body(None),
    ):
        try:
            return studio_meta.save_settings(
                notify_on=notify_on,
                telegram_chat_id=telegram_chat_id,
                weekly_digest=weekly_digest,
                notify_url=notify_url,
                telegram_token=telegram_token,
                home_tabs=home_tabs,
            )
        except ValueError as e:
            _bad(422, str(e))

    @app.post("/api/settings/test")
    def settings_test():
        if not studio_meta.configured():
            _bad(422, "no notification channel configured")
        sent, failed = studio_meta.send("msts-trader Studio · test notification ✓")
        return {"sent": sent, "failed": failed}

    # ── OS task: run schedules without Studio open ───────────────────
    @app.get("/api/os-schedule")
    def os_schedule_status():
        from . import os_schedule

        return os_schedule.status()

    @app.post("/api/os-schedule")
    def os_schedule_set(install: bool = Body(..., embed=True)):
        from . import os_schedule

        try:
            return os_schedule.install() if install else os_schedule.uninstall()
        except os_schedule.ScheduleError as e:
            _bad(422, str(e))

    @app.get("/api/scheduler")
    def scheduler_state():
        sched = getattr(app.state, "scheduler", None)
        return sched.state() if sched else {"running": False}

    # ── SPA ───────────────────────────────────────────────────────────
    if static_dir.exists() and (static_dir / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=static_dir / "assets", check_dir=False), name="assets")

        @app.get("/{path:path}")
        def spa(path: str):
            if path.startswith("api/"):
                _bad(404, "not found")
            f = static_dir / path
            if path and f.is_file() and static_dir in f.resolve().parents:
                return FileResponse(f)
            return FileResponse(static_dir / "index.html")
    else:

        @app.get("/")
        def no_build():
            return JSONResponse(
                {"detail": "UI assets not built. From a source checkout: cd web && npm ci && npm run build"},
                status_code=503,
            )

    return app


def serve(*, port: int = 8765, open_browser: bool = True, scheduler: bool = True) -> None:
    import uvicorn

    token = secrets.token_urlsafe(24)
    origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    app = create_app(token, allowed_origins=origins)
    if scheduler:
        from .scheduler import Scheduler

        app.state.scheduler = Scheduler()
        app.state.scheduler.start()
    url = f"http://127.0.0.1:{port}/?t={token}"
    print(
        f"msts-trader studio → {url}\n(keep this URL private: it carries the session token; Ctrl+C to stop)", flush=True
    )
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
