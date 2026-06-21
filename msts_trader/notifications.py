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

from . import __version__
from .prompts import env_value

# Some webhook hosts sit behind a WAF/CDN (e.g. Cloudflare) that 403s the
# default ``Python-urllib/x.y`` User-Agent. Send a real one so generic
# webhooks (n8n, self-hosted automations) accept the POST.
_USER_AGENT = f"msts-trader/{__version__}"


def _post_json(url: str, payload: dict, timeout: float = 10.0) -> bool:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": _USER_AGENT},
        method="POST",
    )
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


def notify(
    text: str,
    *,
    notify_url: Optional[str] = None,
    telegram_token: Optional[str] = None,
    telegram_chat_id: Optional[str] = None,
) -> tuple[list[str], list[str]]:
    """Send `text` to whatever channels are configured.

    Returns ``(sent, failed)`` — the channels that delivered and the channels
    that were configured but failed to deliver. A channel absent from both was
    simply not configured.

    The explicit args override their env vars when given (e.g. from a config
    file): `notify_url` → MSTS_NOTIFY_URL, `telegram_token` →
    MSTS_TELEGRAM_TOKEN, `telegram_chat_id` → MSTS_TELEGRAM_CHAT_ID.
    """
    sent: list[str] = []
    failed: list[str] = []
    url = notify_url or env_value("MSTS_NOTIFY_URL")
    if url:
        (sent if _send_webhook(url, text) else failed).append("webhook")

    tg_token = telegram_token or env_value("MSTS_TELEGRAM_TOKEN")
    tg_chat = telegram_chat_id or env_value("MSTS_TELEGRAM_CHAT_ID")
    if tg_token and tg_chat:
        (sent if _send_telegram(tg_token, tg_chat, text) else failed).append("telegram")
    return sent, failed


def format_summary(broker: str, account_id: str, sent: int, failed: int, orders: list, *, dry_run: bool = False) -> str:
    """Build a concise human-readable summary line for a rebalance.

    With `dry_run=True` the header announces a preview and that nothing was
    sent, so a webhook/Telegram recipient can't mistake it for a live fill.
    """
    if dry_run:
        head = f"msts-trader · {broker} ({account_id}) · DRY-RUN preview · {len(orders)} orders (nothing sent)"
    else:
        head = f"msts-trader · {broker} ({account_id}) · {sent} filled, {failed} failed"
    lines = [head]
    for o in orders[:20]:
        side = getattr(o, "side", None)
        side = side.value if side is not None else "?"
        lines.append(f"  {side} {getattr(o, 'quantity', '?')} {getattr(o, 'ticker', '?')}")
    if len(orders) > 20:
        lines.append(f"  … +{len(orders) - 20} more")
    return "\n".join(lines)
