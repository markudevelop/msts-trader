"""Studio library features: dashboard, tags, bulk actions, export, go-to-cash,
cross-process scheduler lock, OS task install (mocked), notifications."""

from __future__ import annotations

import io
import json
import os
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from click.testing import CliRunner  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from msts_trader import notifications  # noqa: E402
from msts_trader.__main__ import main  # noqa: E402
from msts_trader.market_hours import ET  # noqa: E402
from msts_trader.symphony import store, studio_meta  # noqa: E402
from msts_trader.symphony.model import Symphony  # noqa: E402
from msts_trader.ui import os_schedule  # noqa: E402
from msts_trader.ui import scheduler as sched  # noqa: E402
from msts_trader.ui.server import create_app  # noqa: E402
from tests.test_studio import TOKEN, H, make_sym, studio_home  # noqa: E402,F401  (autouse: isolates ~/.msts-trader)


@pytest.fixture(autouse=True)
def fake_keyring(monkeypatch):
    vault: dict[tuple[str, str], str] = {}
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda s, k: vault.get((s, k)))
    monkeypatch.setattr(keyring, "set_password", lambda s, k, v: vault.__setitem__((s, k), v))
    monkeypatch.setattr(keyring, "delete_password", lambda s, k: vault.pop((s, k), None))
    return vault


@pytest.fixture
def sent(monkeypatch):
    out: list[str] = []

    def fake_notify(text, **kw):
        out.append(text)
        return (["webhook"], [])

    monkeypatch.setattr(notifications, "notify", fake_notify)
    return out


@pytest.fixture
def client():
    return TestClient(create_app(TOKEN, allowed_origins=set()))


# ── tags ──────────────────────────────────────────────────────────────────
def test_tags_are_normalized():
    s = make_sym().model_copy(update={"tags": []})
    s = Symphony.model_validate(
        {**s.model_dump(by_alias=True), "tags": ["  Momentum ", "momentum", "Defensive  core", ""]}
    )
    assert s.tags == ["Momentum", "Defensive core"]


# ── dashboard ─────────────────────────────────────────────────────────────
def test_dashboard_rows_track_funding_views_backtests_and_runs(client):
    store.save(make_sym("a"))
    store.save(make_sym("b", live_enabled=True))
    client.post("/api/strategies/b/capital", json={"action": "invest", "amount": "10000"}, headers=H())
    client.get("/api/strategies/a", headers=H())  # viewed
    body = store.get("b").model_dump(by_alias=True)
    client.post("/api/backtest", json={"strategy": body, "cost_bps": 0}, headers=H())  # full history -> recorded
    client.post(
        "/api/backtest", json={"strategy": body, "start": "2026-01-01"}, headers=H()
    )  # windowed -> not recorded
    client.post("/api/strategies/b/run", json={"mode": "dry"}, headers=H())

    rows = {r["id"]: r for r in client.get("/api/dashboard", headers=H()).json()["strategies"]}
    assert rows["a"]["funded"] is False and rows["a"]["last_viewed"]
    assert rows["b"]["funded"] is True and rows["b"]["contributed"] == "10000"
    bt = rows["b"]["last_backtest"]
    assert bt["metrics"]["cagr"] is not None and bt["start"] != "2026-01-01"
    assert rows["b"]["last_run"]["status"] == "preview"


# ── bulk / export ─────────────────────────────────────────────────────────
def test_bulk_tag_pause_resume_delete(client):
    for sid in ("a", "b", "c"):
        store.save(make_sym(sid, schedule_enabled=True))
    r = client.post(
        "/api/strategies/bulk", json={"ids": ["a", "b", "zzz"], "action": "tag", "tag": "Momentum"}, headers=H()
    ).json()
    assert r["done"] == ["a", "b"] and r["failed"][0]["id"] == "zzz"
    assert store.get("a").tags == ["Momentum"] and store.get("c").tags == []
    client.post("/api/strategies/bulk", json={"ids": ["a"], "action": "untag", "tag": "momentum"}, headers=H())
    assert store.get("a").tags == []
    client.post("/api/strategies/bulk", json={"ids": ["a", "b"], "action": "pause"}, headers=H())
    assert not store.get("a").deploy.schedule_enabled and store.get("c").deploy.schedule_enabled
    client.post("/api/strategies/bulk", json={"ids": ["a"], "action": "resume"}, headers=H())
    assert store.get("a").deploy.schedule_enabled
    client.post("/api/strategies/bulk", json={"ids": ["b", "c"], "action": "delete"}, headers=H())
    assert [s.id for s in store.list_all()] == ["a"]
    assert client.post("/api/strategies/bulk", json={"ids": ["a"], "action": "nuke"}, headers=H()).status_code == 422


def test_export_zip(client):
    store.save(make_sym("a"))
    store.save(make_sym("b"))
    r = client.post("/api/strategies/export", json={"ids": ["a", "b", "missing"]}, headers=H())
    assert r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(z.namelist()) == ["a.json", "b.json"]
    assert Symphony.model_validate_json(z.read("a.json")).id == "a"


# ── go to cash (real rebalance subprocess on paper) ───────────────────────
def test_go_to_cash_sells_the_sleeve_and_pauses(client):
    store.save(make_sym("a", live_enabled=True, schedule_enabled=True))
    client.post("/api/strategies/a/capital", json={"action": "invest", "amount": "10000"}, headers=H())
    bought = client.post("/api/strategies/a/run", json={"mode": "live", "confirm": "a"}, headers=H()).json()
    assert bought["status"] == "executed", bought

    assert client.post("/api/strategies/cash", json={"ids": ["a"], "confirm": "yes"}, headers=H()).status_code == 403
    # live trading need not be on: going to cash only sells what the sleeve owns
    store.save(
        store.get("a").model_copy(update={"deploy": store.get("a").deploy.model_copy(update={"live_enabled": False})})
    )
    res = client.post("/api/strategies/cash", json={"ids": ["a"], "confirm": "CASH"}, headers=H()).json()["results"][0]
    assert res["status"] == "executed", res
    assert {o["side"] for o in res["preview"]["orders"]} == {"SELL"}
    sleeve = client.get("/api/strategies/a/sleeve", headers=H()).json()["ledgers"][0]
    assert sleeve["holdings"] == {}
    assert store.get("a").deploy.schedule_enabled is False
    again = client.post("/api/strategies/cash", json={"ids": ["a"], "confirm": "CASH"}, headers=H()).json()["results"][
        0
    ]
    assert again["status"] == "nothing-to-do"


# ── scheduler lock ────────────────────────────────────────────────────────
def test_tick_skips_while_another_scheduler_holds_the_lock():
    store.save(make_sym(schedule_enabled=True, schedule_time="15:50"))
    tue = datetime(2026, 9, 29, 15, 55, tzinfo=ET)
    p = sched.lock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("123")
    assert sched.Scheduler().tick(tue) == []  # held -> skip, nothing logged
    assert store.read_runs("momo") == []
    old = datetime.now().timestamp() - 3600
    os.utime(p, (old, old))  # a crashed holder: stale after 30 min
    assert [r["status"] for r in sched.Scheduler().tick(tue)] == ["preview"]
    assert not p.exists()


def test_run_due_cli():
    r = CliRunner().invoke(main, ["strategy", "run-due"])
    assert r.exit_code == 0 and "nothing due" in r.output


# ── OS task (commands only; nothing is really installed) ──────────────────
def test_os_schedule_windows_commands(monkeypatch):
    calls = []
    state = {"installed": False}

    def fake_run(args, input=None):
        calls.append(args)
        if args[:2] == ["schtasks", "/Create"]:
            state["installed"] = True
        if args[:2] == ["schtasks", "/Delete"]:
            state["installed"] = False
        rc = 0 if (args[1] != "/Query" or state["installed"]) else 1

        class R:
            returncode, stdout, stderr = rc, "TaskName: msts-trader-strategies", ""

        return R()

    monkeypatch.setattr(os_schedule, "_windows", lambda: True)
    monkeypatch.setattr(os_schedule, "_run", fake_run)
    assert os_schedule.status()["installed"] is False
    st = os_schedule.install()
    create = next(c for c in calls if c[:2] == ["schtasks", "/Create"])
    assert create[create.index("/SC") + 1] == "MINUTE" and create[create.index("/MO") + 1] == "5"
    assert "strategy run-due" in create[create.index("/TR") + 1] and st["installed"] is True
    assert os_schedule.uninstall()["installed"] is False


def test_os_schedule_cron_keeps_other_lines(monkeypatch):
    tab = {"text": "0 9 * * 1 /usr/bin/backup\n"}

    def fake_run(args, input=None):
        class R:
            returncode, stderr = 0, ""
            stdout = tab["text"]

        if args == ["crontab", "-"]:
            tab["text"] = input
        return R()

    monkeypatch.setattr(os_schedule, "_windows", lambda: False)
    monkeypatch.setattr(os_schedule, "_run", fake_run)
    os_schedule.install()
    os_schedule.install()  # idempotent: still one line
    lines = tab["text"].splitlines()
    assert lines[0] == "0 9 * * 1 /usr/bin/backup" and sum(ln.endswith(os_schedule.MARKER) for ln in lines) == 1
    assert lines[1].startswith("*/5 * * * * ") and "strategy run-due" in lines[1]
    os_schedule.uninstall()
    assert tab["text"] == "0 9 * * 1 /usr/bin/backup\n"


def test_os_schedule_api(client, monkeypatch):
    monkeypatch.setattr(os_schedule, "install", lambda: {"supported": True, "installed": True})
    monkeypatch.setattr(os_schedule, "uninstall", lambda: {"supported": True, "installed": False})
    assert client.post("/api/os-schedule", json={"install": True}, headers=H()).json()["installed"] is True
    assert client.post("/api/os-schedule", json={"install": False}, headers=H()).json()["installed"] is False


# ── notifications ─────────────────────────────────────────────────────────
def test_settings_never_echo_secrets(client, fake_keyring):
    r = client.put(
        "/api/settings",
        json={
            "notify_url": "https://discord.example/hook/SECRET",
            "telegram_token": "TG-SECRET",
            "telegram_chat_id": "42",
        },
        headers=H(),
    ).json()
    assert r["has_notify_url"] and r["has_telegram_token"] and r["telegram_chat_id"] == "42"
    assert "SECRET" not in json.dumps(client.get("/api/settings", headers=H()).json())
    assert "SECRET" not in Path(studio_meta.settings_path()).read_text()
    assert client.put("/api/settings", json={"notify_url": "ftp://x"}, headers=H()).status_code == 422
    assert client.put("/api/settings", json={"notify_url": ""}, headers=H()).json()["has_notify_url"] is False


def test_notify_after_runs_respects_setting(client, sent):
    store.save(make_sym("a", live_enabled=True))
    studio_meta.save_settings(notify_url="https://hook.example/x")
    client.post("/api/strategies/a/run", json={"mode": "dry"}, headers=H())
    assert sent == []  # default: live runs (and errors) only
    client.post("/api/strategies/a/run", json={"mode": "live", "confirm": "a"}, headers=H())
    assert len(sent) == 1 and "Momo (paper) — EXECUTED [LIVE" in sent[0] and "BUY SPY" in sent[0]
    studio_meta.save_settings(notify_on="all")
    client.post("/api/strategies/a/run", json={"mode": "dry"}, headers=H())
    assert len(sent) == 2 and "dry-run" in sent[1]
    studio_meta.save_settings(notify_on="off")
    client.post("/api/strategies/a/run", json={"mode": "dry"}, headers=H())
    assert len(sent) == 2
    assert client.post("/api/settings/test", headers=H()).json()["sent"] == ["webhook"]


def test_weekly_digest_once_per_week_after_friday_close(client, sent):
    store.save(make_sym("a", live_enabled=True))
    client.post("/api/strategies/a/capital", json={"action": "invest", "amount": "10000"}, headers=H())
    studio_meta.save_settings(notify_url="https://hook.example/x", weekly_digest=True)
    fri = datetime(2026, 10, 2, 16, 20, tzinfo=ET)
    assert studio_meta.maybe_send_digest(fri.replace(hour=15)) is False  # before the close
    assert studio_meta.maybe_send_digest(datetime(2026, 10, 1, 17, 0, tzinfo=ET)) is False  # Thursday
    assert studio_meta.maybe_send_digest(fri) is True
    assert "weekly digest" in sent[-1] and "Momo: capital $10,000" in sent[-1]
    assert studio_meta.maybe_send_digest(fri.replace(hour=17)) is False  # once per week
