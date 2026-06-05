"""Fire-and-forget notifications on rebalance/execute.

Supports Discord and Slack webhooks (auto-detected from the URL), generic
JSON webhooks, and Telegram bots. Configured via env / config file:

  MSTS_NOTIFY_URL          Discord, Slack, or generic webhook URL
  MSTS_TELEGRAM_TOKEN      Telegram bot token   (with MSTS_TELEGRAM_CHAT_ID)
  MSTS_TELEGRAM_CHAT_ID    Telegram chat id

Notification failures NEVER raise — a down webhook must not break trading.
Uses only the stdlib (urllib), no extra dependency.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Optional

from .prompts import env_value


def _post_json(url: str, payload: dict, timeout: float = 10.0) -> bool:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout):  # noqa: S310
            return True
    except Exception:
        return False


def _send_webhook(url: str, text: str) -> bool:
    low = url.lower()
    if "discord.com" in low or "discordapp.com" in low:
        return _post_json(url, {"content": text[:1900]})
    if "hooks.slack.com" in low:
        return _post_json(url, {"text": text})
    # generic webhook
    return _post_json(url, {"text": text})


def _send_telegram(token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    return _post_json(url, {"chat_id": chat_id, "text": text})


def notify(text: str, *, notify_url: Optional[str] = None) -> list[str]:
    """Send `text` to whatever channels are configured. Returns channels hit.

    notify_url overrides MSTS_NOTIFY_URL when given (e.g. from a config file).
    """
    sent: list[str] = []
    url = notify_url or env_value("MSTS_NOTIFY_URL")
    if url and _send_webhook(url, text):
        sent.append("webhook")

    tg_token = env_value("MSTS_TELEGRAM_TOKEN")
    tg_chat = env_value("MSTS_TELEGRAM_CHAT_ID")
    if tg_token and tg_chat and _send_telegram(tg_token, tg_chat, text):
        sent.append("telegram")
    return sent


def format_summary(broker: str, account_id: str, sent: int, failed: int, orders: list) -> str:
    """Build a concise human-readable summary line for a rebalance."""
    lines = [f"msts-trader · {broker} ({account_id}) · {sent} filled, {failed} failed"]
    for o in orders[:20]:
        side = getattr(o, "side", None)
        side = side.value if side is not None else "?"
        lines.append(f"  {side} {getattr(o, 'quantity', '?')} {getattr(o, 'ticker', '?')}")
    if len(orders) > 20:
        lines.append(f"  … +{len(orders) - 20} more")
    return "\n".join(lines)
