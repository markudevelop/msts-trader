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

import json
import os
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

from .. import __version__, sleeves
from ..brokers import SUPPORTED
from ..market_hours import market_status
from ..symphony import backtest, composer_import, prices, runner, store
from ..symphony.evaluate import EvalError
from ..symphony.model import INDICATORS, Symphony, slugify, tickers

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


def _sleeve_state(sid: str, broker: str) -> list[dict]:
    """Every ledger (account) of `broker` that knows this sleeve. File-only —
    no broker login needed; values use the last cached close."""
    out = []
    if not sleeves.LEDGER_DIR.exists():
        return out
    for p in sorted(sleeves.LEDGER_DIR.glob(f"{broker}_*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        book = (data.get("sleeves") or {}).get(sid)
        cash = (data.get("cash") or {}).get(sid)
        if book is None and cash is None:
            continue
        out.append(
            {
                "account": data.get("account"),
                "cash": cash,
                "contributed": (data.get("contributed") or {}).get(sid),
                "holdings": {t: q for t, q in sorted((book or {}).items()) if Decimal(q) != 0},
                "pending": [x for x in data.get("pending") or [] if x.get("sleeve") == sid],
            }
        )
    return out


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
        return _get(sid).model_dump(by_alias=True)

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
            return backtest.run(s, closes, start=d0, end=d1, cost_bps=cost_bps, benchmark=bm)
        except (EvalError, prices.PriceError) as e:
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
        return {**res, "sleeve": _sleeve_state(s.id, s.deploy.broker)}

    @app.get("/api/strategies/{sid}/sleeve")
    def sleeve_state(sid: str):
        s = _get(sid)
        return {"broker": s.deploy.broker, "ledgers": _sleeve_state(s.id, s.deploy.broker)}

    @app.get("/api/runs")
    def runs(strategy: str | None = None, limit: int = 50):
        return store.read_runs(strategy, limit=min(max(limit, 1), 500))

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
