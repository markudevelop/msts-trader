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
    sent, failed = notifications.notify("hi", notify_url="https://discord.com/api/webhooks/x")
    assert "webhook" in sent and failed == []
    assert calls["url"].startswith("https://discord.com")


def test_notify_webhook_failure_reported(monkeypatch):
    monkeypatch.setattr(notifications, "_send_webhook", lambda url, text: False)
    monkeypatch.setattr(notifications, "env_value", lambda k: None)
    sent, failed = notifications.notify("hi", notify_url="https://example.com/hook")
    assert sent == [] and "webhook" in failed


def test_notify_nothing_configured(monkeypatch):
    monkeypatch.setattr(notifications, "env_value", lambda k: None)
    assert notifications.notify("hi") == ([], [])


def test_notify_telegram(monkeypatch):
    env = {"MSTS_TELEGRAM_TOKEN": "tok", "MSTS_TELEGRAM_CHAT_ID": "123"}
    monkeypatch.setattr(notifications, "env_value", lambda k: env.get(k))
    monkeypatch.setattr(notifications, "_send_telegram", lambda t, c, text: True)
    sent, failed = notifications.notify("hi")
    assert "telegram" in sent and failed == []


def test_notify_telegram_explicit_args_override_env(monkeypatch):
    # config.toml path: creds come in as explicit args, no env set.
    captured = {}
    monkeypatch.setattr(notifications, "env_value", lambda k: None)
    monkeypatch.setattr(
        notifications, "_send_telegram",
        lambda t, c, text: captured.update(token=t, chat=c) or True,
    )
    sent, _ = notifications.notify("hi", telegram_token="TOK", telegram_chat_id="CID")
    assert "telegram" in sent
    assert captured == {"token": "TOK", "chat": "CID"}


def test_format_summary_dry_run_announces_preview():
    from decimal import Decimal
    from msts_trader.models import Order, Side
    orders = [Order(ticker="SPY", side=Side.BUY, quantity=Decimal("10"))]
    s = notifications.format_summary("ibkr", "ACC", 0, 0, orders, dry_run=True)
    assert "DRY-RUN" in s and "nothing sent" in s and "BUY 10 SPY" in s


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


def test_post_json_sends_real_user_agent(monkeypatch):
    # The default Python-urllib User-Agent gets 403'd by WAFs/CDNs (Cloudflare,
    # n8n behind a proxy), so generic webhooks silently failed. We must send a
    # real User-Agent identifying msts-trader.
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=10.0):
        captured["ua"] = req.get_header("User-agent")
        return _Resp()

    monkeypatch.setattr(notifications.urllib.request, "urlopen", fake_urlopen)
    assert notifications._post_json("https://example.com/hook", {"text": "hi"}) is True
    assert captured["ua"] and "msts-trader" in captured["ua"]
    assert "Python-urllib" not in captured["ua"]
