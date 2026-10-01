"""Studio bookkeeping that isn't part of a strategy's definition.

~/.msts-trader/studio_meta.json      (override: MSTS_STUDIO_META)
    {"strategies": {id: {"last_viewed": ts, "last_backtest": {...}}},
     "digest": {"last_week": "2026-W40"}}
~/.msts-trader/studio_settings.json  (override: MSTS_STUDIO_SETTINGS)
    non-secret notification preferences; the webhook URL and Telegram token
    live in the OS keychain.

Also: per-run notifications and the weekly digest, sent with Studio's own
settings (never via env vars, so the `rebalance` subprocess doesn't send a
second, generic message).
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

_LOCK = threading.Lock()
_KEYRING_SERVICE = "msts-trader"
_SECRET_KEYS = {"notify_url": "studio-notify-url", "telegram_token": "studio-telegram-token"}
DEFAULT_SETTINGS = {"notify_on": "live", "telegram_chat_id": "", "weekly_digest": False}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def meta_path() -> Path:
    return Path(os.environ.get("MSTS_STUDIO_META") or os.path.expanduser("~/.msts-trader/studio_meta.json"))


def settings_path() -> Path:
    return Path(os.environ.get("MSTS_STUDIO_SETTINGS") or os.path.expanduser("~/.msts-trader/studio_settings.json"))


def _read(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}


def _write(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True, default=str), encoding="utf-8")
    os.replace(tmp, p)


# ── per-strategy meta ──────────────────────────────────────────────────────
def all_meta() -> dict:
    return _read(meta_path()).get("strategies", {})


def update(sid: str, **fields) -> None:
    with _LOCK:
        data = _read(meta_path())
        data.setdefault("strategies", {}).setdefault(sid, {}).update(fields)
        _write(meta_path(), data)


def forget(sid: str) -> None:
    with _LOCK:
        data = _read(meta_path())
        data.get("strategies", {}).pop(sid, None)
        _write(meta_path(), data)


def mark_viewed(sid: str) -> None:
    update(sid, last_viewed=_now())


def record_backtest(sid: str, res: dict) -> None:
    bm = res.get("benchmark") or {}
    update(
        sid,
        last_backtest={
            "ts": _now(),
            "start": res.get("start"),
            "end": res.get("end"),
            "cost_bps": res.get("cost_bps"),
            "metrics": res.get("metrics"),
            "benchmark": {"ticker": bm.get("ticker"), "metrics": bm.get("metrics")} if bm else None,
        },
    )


def digest_week() -> str | None:
    return _read(meta_path()).get("digest", {}).get("last_week")


def set_digest_week(week: str) -> None:
    with _LOCK:
        data = _read(meta_path())
        data.setdefault("digest", {})["last_week"] = week
        _write(meta_path(), data)


# ── notification settings ──────────────────────────────────────────────────
def _secret(name: str) -> str | None:
    try:
        import keyring

        return keyring.get_password(_KEYRING_SERVICE, _SECRET_KEYS[name]) or None
    except Exception:
        return None


def _set_secret(name: str, value: str | None) -> None:
    import keyring

    if value:
        keyring.set_password(_KEYRING_SERVICE, _SECRET_KEYS[name], value.strip())
    else:
        try:
            keyring.delete_password(_KEYRING_SERVICE, _SECRET_KEYS[name])
        except Exception:
            pass


def get_settings() -> dict:
    """Public view: flags plus has_* booleans — never the secrets themselves."""
    s = {**DEFAULT_SETTINGS, **_read(settings_path())}
    return {
        "notify_on": s["notify_on"],
        "telegram_chat_id": s.get("telegram_chat_id") or "",
        "weekly_digest": bool(s.get("weekly_digest")),
        "has_notify_url": bool(_secret("notify_url")),
        "has_telegram_token": bool(_secret("telegram_token")),
    }


def save_settings(
    *, notify_on=None, telegram_chat_id=None, weekly_digest=None, notify_url=None, telegram_token=None
) -> dict:
    """None = leave unchanged; "" for a secret = clear it."""
    s = {**DEFAULT_SETTINGS, **_read(settings_path())}
    if notify_on is not None:
        if notify_on not in ("off", "live", "all"):
            raise ValueError("notify_on must be off, live or all")
        s["notify_on"] = notify_on
    if telegram_chat_id is not None:
        s["telegram_chat_id"] = str(telegram_chat_id).strip()
    if weekly_digest is not None:
        s["weekly_digest"] = bool(weekly_digest)
    _write(settings_path(), {k: s[k] for k in DEFAULT_SETTINGS})
    if notify_url is not None:
        if notify_url and not notify_url.lower().startswith(("http://", "https://")):
            raise ValueError("webhook URL must be http(s)")
        _set_secret("notify_url", notify_url)
    if telegram_token is not None:
        _set_secret("telegram_token", telegram_token)
    return get_settings()


def send(text: str) -> tuple[list[str], list[str]]:
    from .. import notifications

    s = {**DEFAULT_SETTINGS, **_read(settings_path())}
    return notifications.notify(
        text,
        notify_url=_secret("notify_url"),
        telegram_token=_secret("telegram_token"),
        telegram_chat_id=s.get("telegram_chat_id") or None,
    )


def configured() -> bool:
    s = get_settings()
    return s["has_notify_url"] or (s["has_telegram_token"] and bool(s["telegram_chat_id"]))


# ── message formatting ─────────────────────────────────────────────────────
def _money(v) -> str:
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return "?"


def format_run(entry: dict, name: str) -> str:
    status = str(entry.get("status", "?")).upper()
    mode = "LIVE" if entry.get("mode") == "live" else "dry-run"
    head = f"msts-trader · {name} ({entry.get('broker')}) — {status} [{mode}, {entry.get('source', '')}]"
    lines = [head]
    if entry.get("error"):
        lines.append(f"⚠ {entry['error']}")
    orders = (entry.get("preview") or {}).get("orders") or []
    for o in orders[:15]:
        lines.append(f"{o.get('side')} {o.get('ticker')} {o.get('quantity')} (~{_money(o.get('notional'))})")
    if len(orders) > 15:
        lines.append(f"… and {len(orders) - 15} more")
    ex = entry.get("execution") or {}
    if ex:
        lines.append(f"sent {ex.get('sent', 0)}, failed {ex.get('failed', 0)}")
    if not orders and entry.get("status") == "nothing-to-do":
        lines.append("on target — no orders")
    return "\n".join(lines)


def notify_run(entry: dict, name: str) -> None:
    """After a run: per the notify_on setting. Never raises."""
    try:
        s = get_settings()
        if s["notify_on"] == "off" or not configured():
            return
        if s["notify_on"] == "live" and entry.get("mode") != "live" and entry.get("status") != "error":
            return
        send(format_run(entry, name))
    except Exception:
        pass


def build_digest(rows: list[dict]) -> str:
    lines = ["msts-trader · weekly digest"]
    for r in rows:
        pnl = r.get("pnl")
        pnl_s = "" if pnl is None else f" · P&L {'+' if pnl >= 0 else '-'}{_money(abs(pnl))}"
        last = r.get("last_run") or {}
        lines.append(
            f"• {r['name']}: capital {_money(r.get('contributed'))}, NAV {_money(r.get('nav'))}{pnl_s}"
            f" · last run {last.get('status', '—')} {str(last.get('ts', ''))[:10]}"
        )
    if len(lines) == 1:
        lines.append("no funded strategies")
    return "\n".join(lines)


def funded_rows() -> list[dict]:
    """Funded strategies with capital, NAV (last close) and P&L — for the digest."""
    from . import performance, prices, store

    runs = {}
    for e in reversed(store.read_runs(limit=100000)):  # oldest -> newest, keep the latest
        runs[e.get("strategy")] = e
    out = []
    holdings_needed = set()
    staged = []
    for s in store.list_all():
        led = performance._pick(performance.sleeve_ledgers(s.id, s.deploy.broker), s.deploy.account)
        if not led or not (led.get("contributed") or led.get("cash") or led.get("holdings")):
            continue
        holdings_needed |= set(led["holdings"])
        staged.append((s, led))
    closes = None
    if holdings_needed:
        try:
            closes = prices.load_closes(sorted(holdings_needed))
        except Exception:
            closes = None
    for s, led in staged:
        nav = float(led.get("cash") or 0)
        ok = True
        for t, q in led["holdings"].items():
            if closes is None or t not in closes.columns or closes[t].dropna().empty:
                ok = False
                break
            nav += float(Decimal(q)) * float(closes[t].dropna().iloc[-1])
        contributed = float(led["contributed"]) if led.get("contributed") not in (None, "") else None
        out.append(
            {
                "id": s.id,
                "name": s.name,
                "contributed": contributed,
                "nav": nav if ok else None,
                "pnl": (nav - contributed) if ok and contributed is not None else None,
                "last_run": runs.get(s.id),
            }
        )
    return out


def _week(now) -> str:
    iso = now.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def digest_due(now) -> bool:
    """Fridays after 16:15 ET, once per ISO week, when enabled and configured."""
    from datetime import time

    try:
        if not get_settings()["weekly_digest"] or not configured():
            return False
        if now.weekday() != 4 or now.timetz().replace(tzinfo=None) < time(16, 15):
            return False
        return digest_week() != _week(now)
    except Exception:
        return False


def maybe_send_digest(now) -> bool:
    """Send the weekly digest if it's due. Never raises."""
    try:
        if not digest_due(now):
            return False
        week = _week(now)
        sent, _ = send(build_digest(funded_rows()))
        if sent:
            set_digest_week(week)
        return bool(sent)
    except Exception:
        return False
