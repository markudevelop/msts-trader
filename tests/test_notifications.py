from __future__ import annotations

from decimal import Decimal

from msts_trader import notifications
from msts_trader.models import Order, Side


def test_format_summary_basic():
    orders = [Order(ticker="SPY", side=Side.BUY, quantity=Decimal("10"))]
    s = notifications.format_summary("tastytrade", "ACC", 1, 0, orders)
    assert "tastytrade" in s and "1 filled" in s and "BUY 10 SPY" in s


def test_format_summary_truncates(monkeypatch):
    orders = [Order(ticker=f"T{i}", side=Side.BUY, quantity=Decimal("1")) for i in range(30)]
    s = notifications.format_summary("alpaca", "A", 30, 0, orders)
    assert "more" in s


def test_notify_webhook_called(monkeypatch):
    calls = {}
    monkeypatch.setattr(notifications, "_send_webhook", lambda url, text: calls.setdefault("url", url) or True)
    monkeypatch.setattr(notifications, "env_value", lambda k: None)
    out = notifications.notify("hi", notify_url="https://discord.com/api/webhooks/x")
    assert "webhook" in out
    assert calls["url"].startswith("https://discord.com")


def test_notify_nothing_configured(monkeypatch):
    monkeypatch.setattr(notifications, "env_value", lambda k: None)
    assert notifications.notify("hi") == []


def test_notify_telegram(monkeypatch):
    env = {"MSTS_TELEGRAM_TOKEN": "tok", "MSTS_TELEGRAM_CHAT_ID": "123"}
    monkeypatch.setattr(notifications, "env_value", lambda k: env.get(k))
    monkeypatch.setattr(notifications, "_send_telegram", lambda t, c, text: True)
    out = notifications.notify("hi")
    assert "telegram" in out


def test_webhook_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr(notifications, "_post_json", lambda url, payload, timeout=10.0: False)
    # should return False, not raise
    assert notifications._send_webhook("https://hooks.slack.com/x", "t") is False


def test_discord_payload_uses_content(monkeypatch):
    captured = {}
    monkeypatch.setattr(notifications, "_post_json", lambda url, payload, timeout=10.0: captured.update(payload) or True)
    notifications._send_webhook("https://discord.com/api/webhooks/x", "hello")
    assert "content" in captured and captured["content"] == "hello"


def test_slack_payload_uses_text(monkeypatch):
    captured = {}
    monkeypatch.setattr(notifications, "_post_json", lambda url, payload, timeout=10.0: captured.update(payload) or True)
    notifications._send_webhook("https://hooks.slack.com/services/x", "hello")
    assert captured.get("text") == "hello"


def test_generic_webhook_uses_text(monkeypatch):
    captured = {}
    monkeypatch.setattr(notifications, "_post_json", lambda url, payload, timeout=10.0: captured.update(payload) or True)
    notifications._send_webhook("https://example.com/hook", "hello")
    assert captured.get("text") == "hello"
