"""Evaluate a strategy and hand its weights to the existing rebalance engine.

The trading path is deliberately NOT reimplemented: a strategy run writes a
`ticker,weight` CSV and invokes

    msts-trader rebalance --csv-file F --sleeve <strategy id> --json ...

as a subprocess, so every guard the CLI already has (sleeve ledger, margin
sizing, max-notional, idempotency, session checks, post-trade verify and
self-heal) applies to UI and scheduled runs unchanged. The strategy id is its
sleeve name; capital comes from `sleeve invest` (compounding, like Composer).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ..market_hours import ET
from . import prices, store
from .evaluate import EvalError, evaluate, max_lookback, to_csv
from .model import Symphony, tickers

LIVE = "live"
DRY = "dry"
_TIMEOUT = 15 * 60  # a limit-chase + self-heal run can take minutes


class RunError(RuntimeError):
    pass


def history_start(sym: Symphony, today: date | None = None) -> date:
    """Calendar start date that covers the strategy's warm-up with margin."""
    today = today or datetime.now(ET).date()
    days = int(max_lookback(sym) * 1.6) + 60
    return today - timedelta(days=days)


def current_weights(sym: Symphony, closes=None) -> dict:
    """{'asof': 'YYYY-MM-DD', 'weights': {ticker: w}, 'csv': str}."""
    if closes is None:
        closes = prices.load_closes(tickers(sym), history_start(sym))
    weights, asof = evaluate(sym, closes)
    ts = tickers(sym)
    csv = to_csv(weights, asof=datetime.now(timezone.utc), fallback=ts[0] if ts else None)
    return {"asof": asof, "weights": {k: round(v, 6) for k, v in sorted(weights.items())}, "csv": csv}


def rebalance_cmd(sym: Symphony, csv_path: str, *, mode: str, force: bool = False) -> list[str]:
    d = sym.deploy
    cmd = [
        sys.executable,
        "-m",
        "msts_trader",
        "rebalance",
        "--broker",
        d.broker,
        "--csv-file",
        csv_path,
        "--sleeve",
        sym.id,
        "--threshold",
        str(d.threshold),
        "--threshold-mode",
        "position",
        "--json",
    ]
    if d.account:
        cmd += ["--account", d.account]
    if mode == LIVE:
        cmd.append("--yes")
    else:
        cmd.append("--dry-run")
    if force:
        cmd.append("--force")
    return cmd


def _parse_json_lines(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _subprocess_env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"  # Windows cp1252 consoles choke on the CLI's glyphs
    return env


def run(
    sym: Symphony, *, mode: str = DRY, force: bool = False, source: str = "cli", closes=None, tags: dict | None = None
) -> dict:
    """Evaluate + rebalance. Never raises for trading outcomes: the result
    (and the run log entry) carries status/error instead."""
    if mode not in (DRY, LIVE):
        raise RunError(f"invalid mode {mode!r}")
    if mode == LIVE and not sym.deploy.live_enabled:
        raise RunError(f"strategy '{sym.id}' is not enabled for live trading (deploy.live_enabled = false)")

    entry: dict = {"strategy": sym.id, "mode": mode, "source": source, "broker": sym.deploy.broker, **(tags or {})}
    try:
        cur = current_weights(sym, closes)
    except (EvalError, prices.PriceError) as e:
        entry.update(status="error", error=f"evaluation failed: {e}")
        return store.log_run(entry)
    entry["asof"] = cur["asof"]
    entry["weights"] = cur["weights"]

    fd, path = tempfile.mkstemp(prefix=f"msts-{sym.id}-", suffix=".csv")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(cur["csv"])
        cmd = rebalance_cmd(sym, path, mode=mode, force=force)
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", timeout=_TIMEOUT, env=_subprocess_env()
            )
        except subprocess.TimeoutExpired:
            entry.update(status="error", error=f"rebalance timed out after {_TIMEOUT // 60} min")
            return store.log_run(entry)
    finally:
        Path(path).unlink(missing_ok=True)

    payloads = _parse_json_lines(proc.stdout)
    preview = next((p for p in payloads if "orders" in p and "executed" not in p), None)
    executed = next((p for p in payloads if p.get("executed")), None)
    error = next((p["error"] for p in payloads if "error" in p), None)
    entry["preview"] = preview
    if executed:
        entry["execution"] = executed
    if error:
        entry.update(status="error", error=error)
    elif proc.returncode != 0 and not preview:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
        entry.update(status="error", error="rebalance failed: " + " | ".join(tail))
    elif preview and preview.get("blockers"):
        entry.update(status="blocked", error="; ".join(preview["blockers"]))
    elif executed:
        entry["status"] = "executed" if not executed.get("failed") else "partial"
    elif preview and not preview.get("orders"):
        entry["status"] = "nothing-to-do"
    elif preview and preview.get("duplicate_today") and mode == LIVE:
        entry["status"] = "duplicate"
    else:
        entry["status"] = "preview" if mode == DRY else "error"
        if mode == LIVE:
            entry.setdefault("error", "live run produced no execution result")
    if mode == LIVE and entry.get("status") in ("executed", "partial"):
        from .performance import snapshot  # local: performance imports runner's siblings

        snapshot(sym, event="run")
    return store.log_run(entry)
