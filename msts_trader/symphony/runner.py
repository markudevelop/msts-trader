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
from . import feeds, prices, store
from .evaluate import EvalError, evaluate, max_lookback, to_csv
from .model import Symphony, feed_nodes, is_feed_key, tickers

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
    nodes = feed_nodes(sym)
    books = {n.ref: feeds.live_weights(n) for n in nodes}
    weights, asof = evaluate(sym, closes, feeds={r: v["weights"] for r, v in books.items()} or None)
    today = datetime.now(ET).date()
    for n in nodes:  # URL feeds without a history URL build their own track record
        feeds.record_weights(n, books[n.ref]["weights"], today)
    real = [t for t in tickers(sym) if not is_feed_key(t)]
    real += [t for v in books.values() for t in sorted(v["weights"])]
    csv = to_csv(weights, asof=datetime.now(timezone.utc), fallback=real[0] if real else None)
    out = {"asof": asof, "weights": {k: round(v, 6) for k, v in sorted(weights.items())}, "csv": csv}
    if books:
        labels = {n.ref: n.label for n in nodes}
        out["feeds"] = {labels[r]: v["date"] for r, v in books.items()}
        # A feed's live book is newer than its published NAV series; report the book's date.
        out["asof"] = max([asof] + [str(v["date"]) for v in books.values() if v.get("date")])
        if not any(v.get("date") for v in books.values()) and not [t for t in tickers(sym) if not is_feed_key(t)]:
            out["asof"] = today.isoformat()
        dropped = sorted(labels[r] for r, v in books.items() if v["stops"])
        if dropped:
            out["warnings"] = [
                f"protective stops published with {', '.join(dropped)} are not placed (sleeves don't place stops yet)"
            ]
    return out


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
        # Explicit either way: a `moc = true` in config.toml must not turn a
        # market strategy into MOC (and get it refused near the close).
        "--moc" if d.order_type == "moc" else "--no-moc",
        "--order-type",
        "limit-chase" if d.order_type in ("limit-chase", "extended") else "market",
        # Extended hours is limit-only (the engine forces --no-chase-fallback).
        # Otherwise unattended runs finish every leg: `extended_hours = true` or
        # `chase_fallback = false` in config.toml would leave remainders unfilled.
        "--extended-hours" if d.order_type == "extended" else "--no-extended-hours",
        "--chase-fallback",
        # Same for the sizing options: whatever config.toml says about whole
        # shares / min weight / scope, the Deploy tab decides. A stray
        # `min_weight = 0.02` there would otherwise stop small targets from ever
        # being bought. MOC and brokers without fractional trading still round
        # to whole shares.
        "--whole-shares" if d.whole_shares or d.order_type == "moc" else "--fractional",
        "--min-weight",
        f"{d.min_weight:g}",
        "--rebalance-scope",
        d.rebalance_scope,
    ]
    if d.max_notional is not None:
        cmd += ["--max-notional", str(d.max_notional)]
    for flag, val in (
        ("--chase-retries", d.chase_retries),
        ("--chase-interval", d.chase_interval),
        ("--chase-aggression", d.chase_aggression),
    ):
        if val is not None:
            cmd += [flag, str(val)]
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
    sym: Symphony,
    *,
    mode: str = DRY,
    force: bool = False,
    source: str = "cli",
    closes=None,
    tags: dict | None = None,
    target: dict | None = None,
) -> dict:
    """Evaluate + rebalance. Never raises for trading outcomes: the result
    (and the run log entry) carries status/error instead.

    `target` skips evaluation and trades the sleeve to these weights instead —
    `{}` is "go to cash" (sell everything this strategy's sleeve holds), which
    is allowed even when live trading is otherwise off for the strategy."""
    if mode not in (DRY, LIVE):
        raise RunError(f"invalid mode {mode!r}")
    if mode == LIVE and not sym.deploy.live_enabled and target is None:
        raise RunError(f"strategy '{sym.id}' is not enabled for live trading (deploy.live_enabled = false)")

    entry: dict = {"strategy": sym.id, "mode": mode, "source": source, "broker": sym.deploy.broker, **(tags or {})}
    if target is not None:
        entry["target"] = "cash" if not target else "override"
        return _finish(sym, entry, _run_target(sym, entry, target, mode=mode, force=force))
    try:
        cur = current_weights(sym, closes)
    except (EvalError, prices.PriceError, feeds.FeedError) as e:
        entry.update(status="error", error=f"evaluation failed: {e}")
        return _finish(sym, entry, store.log_run(entry))
    entry["asof"] = cur["asof"]
    entry["weights"] = cur["weights"]
    for k in ("feeds", "warnings"):
        if k in cur:
            entry[k] = cur[k]

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
    return _finish(sym, entry, store.log_run(entry))


def _finish(sym: Symphony, entry: dict, logged: dict) -> dict:
    from . import studio_meta

    studio_meta.notify_run(logged, sym.name)
    return logged


def _run_target(sym: Symphony, entry: dict, target: dict, *, mode: str, force: bool) -> dict:
    """Trade the sleeve straight to `target` weights (no evaluation)."""
    from .performance import _pick, sleeve_ledgers, snapshot

    led = _pick(sleeve_ledgers(sym.id, sym.deploy.broker), sym.deploy.account)
    held = sorted((led or {}).get("holdings") or {})
    entry["weights"] = {k: round(v, 6) for k, v in sorted(target.items())}
    if not target and not held:
        entry["status"] = "nothing-to-do"
        entry["detail"] = "the strategy's sleeve holds nothing"
        return store.log_run(entry)
    csv = to_csv(target, asof=datetime.now(timezone.utc), fallback=held[0] if held else None)
    fd, path = tempfile.mkstemp(prefix=f"msts-{sym.id}-", suffix=".csv")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(csv)
        proc = subprocess.run(
            rebalance_cmd(sym, path, mode=mode, force=True if not target else force),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_TIMEOUT,
            env=_subprocess_env(),
        )
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
    elif preview and preview.get("blockers"):
        entry.update(status="blocked", error="; ".join(preview["blockers"]))
    elif executed:
        entry["status"] = "executed" if not executed.get("failed") else "partial"
    elif preview and not preview.get("orders"):
        entry["status"] = "nothing-to-do"
    elif proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
        entry.update(status="error", error="rebalance failed: " + " | ".join(tail))
    else:
        entry["status"] = "preview"
    if mode == LIVE and entry.get("status") in ("executed", "partial"):
        snapshot(sym, event="cash" if not target else "run")
    return store.log_run(entry)
