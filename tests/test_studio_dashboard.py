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
    assert create[create.index("/SC") + 1] == "MINUTE" and create[create.index("/MO") + 1] == "1"
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
    assert lines[1].startswith("* * * * * ") and "strategy run-due" in lines[1]
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


# ── every-minute task: outdated installs, cheap no-op check ───────────────
def test_windows_outdated_install_detected_from_xml(monkeypatch):
    xml = {"interval": "PT5M"}

    def fake_run(args, input=None):
        class R:
            returncode, stderr = 0, ""
            stdout = (
                f"<Task><Repetition><Interval>{xml['interval']}</Interval></Repetition></Task>"
                if "/XML" in args
                else "ok"
            )

        return R()

    monkeypatch.setattr(os_schedule, "_windows", lambda: True)
    monkeypatch.setattr(os_schedule, "_run", fake_run)
    st = os_schedule.status()
    assert st["installed_every_minutes"] == 5 and st["outdated"] is True and st["every_minutes"] == 1
    xml["interval"] = "PT1M"
    assert os_schedule.status()["outdated"] is False
    xml["interval"] = "PT1H"
    assert os_schedule.status()["installed_every_minutes"] == 60


def test_cron_outdated_install_detected(monkeypatch):
    tab = {"text": "*/5 * * * * /x/python -m msts_trader strategy run-due >/dev/null 2>&1 # msts-trader-strategies\n"}

    def fake_run(args, input=None):
        class R:
            returncode, stderr = 0, ""
            stdout = tab["text"]

        if args == ["crontab", "-"]:
            tab["text"] = input
        return R()

    monkeypatch.setattr(os_schedule, "_windows", lambda: False)
    monkeypatch.setattr(os_schedule, "_run", fake_run)
    assert os_schedule.status()["outdated"] is True
    st = os_schedule.install()  # reinstall replaces the old line
    assert st["outdated"] is False and st["installed_every_minutes"] == 1
    assert sum(1 for ln in tab["text"].splitlines() if ln.endswith(os_schedule.MARKER)) == 1


def test_anything_due_matches_due_and_digest(sent):
    tue = datetime(2026, 9, 29, 15, 55, tzinfo=ET)
    assert sched.anything_due(tue) is False  # no strategies
    store.save(make_sym(schedule_enabled=True, schedule_time="15:50"))
    assert sched.anything_due(tue) is True and sched.anything_due(tue.replace(hour=15, minute=40)) is False
    sched.Scheduler().tick(tue)
    assert sched.anything_due(tue) is False  # ran this period
    studio_meta.save_settings(notify_url="https://hook.example/x", weekly_digest=True)
    fri = datetime(2026, 10, 2, 16, 20, tzinfo=ET)
    assert sched.anything_due(fri) is True  # digest pending
    studio_meta.maybe_send_digest(fri)
    assert sched.anything_due(fri) is False


def test_run_due_noop_does_not_load_the_engine():
    import subprocess
    import sys as _sys

    code = (
        "import sys; from msts_trader.__main__ import main; sys.argv=['msts-trader','strategy','run-due'];"
        "main(standalone_mode=False); print('PANDAS', 'pandas' in sys.modules)"
    )
    r = subprocess.run([_sys.executable, "-c", code], capture_output=True, text=True, env=os.environ.copy())
    assert r.returncode == 0, r.stderr
    assert "nothing due" in r.stdout and "PANDAS False" in r.stdout


# ── dashboard: held vs target, last check, combined portfolio ─────────────
def test_dashboard_rollup_targets_holdings_and_last_check(client):
    from msts_trader.symphony import prices

    # two funded strategies: momo (60% SPY / 40% TLT) previews only; bond is live and holds TLT
    store.save(make_sym("momo"))
    bond = Symphony.model_validate(
        {
            "id": "bond",
            "name": "Bond",
            "children": [{"step": "asset", "ticker": "TLT"}],
            "deploy": {"broker": "paper", "live_enabled": True},
        }
    )
    store.save(bond)
    store.save(make_sym("idle"))  # unfunded: not in the roll-up
    for sid, amt in (("momo", "10000"), ("bond", "5000")):
        client.post(f"/api/strategies/{sid}/capital", json={"action": "invest", "amount": amt}, headers=H())
    client.post("/api/strategies/momo/run", json={"mode": "dry"}, headers=H())
    client.post("/api/strategies/bond/run", json={"mode": "live", "confirm": "bond"}, headers=H())

    d = client.get("/api/dashboard", headers=H()).json()
    rows = {r["id"]: r for r in d["strategies"]}
    assert rows["momo"]["positions"] == 0 and rows["momo"]["target_positions"] == 2
    assert rows["momo"]["last_run"]["status"] == "preview" and rows["momo"]["last_run"]["orders"] == 2
    assert rows["bond"]["positions"] == 1 and rows["bond"]["last_run"]["status"] == "executed"

    r = d["rollups"]["incubation"]
    assert r["strategies"] == 2 and r["with_targets"] == 2
    t = {x["ticker"]: x for x in r["tickers"]}
    # no cached prices in this isolated home -> capital falls back to contributed
    assert t["SPY"]["target_value"] == pytest.approx(0.6 * 10000)
    assert t["TLT"]["target_value"] == pytest.approx(0.4 * 10000 + 1.0 * 5000)
    assert {b["id"] for b in t["TLT"]["by"]} == {"momo", "bond"}
    assert t["TLT"]["held_qty"] > 0 and t["TLT"]["priced"] is False
    assert r["total_capital"] == pytest.approx(15000)

    # with a cached close, holdings are valued and capital becomes sleeve NAV
    held = t["TLT"]["held_qty"]
    cache = prices.cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "TLT.csv").write_text("date,close\n2026-09-30,100.0\n", encoding="utf-8")
    d2 = client.get("/api/dashboard", headers=H()).json()
    t2 = {x["ticker"]: x for x in d2["rollups"]["incubation"]["tickers"]}
    assert t2["TLT"]["held_value"] == pytest.approx(held * 100.0) and t2["TLT"]["priced"] is True
    bond_nav = {x["id"]: x for x in d2["strategies"]}["bond"]["nav"]
    assert bond_nav == pytest.approx(
        float({x["id"]: x for x in d2["strategies"]}["bond"]["cash"]) + held * 100.0, abs=0.01
    )
    assert t2["TLT"]["target_value"] == pytest.approx(0.4 * 10000 + bond_nav, abs=0.01)


def test_rollup_after_go_to_cash_targets_cash(client):
    store.save(make_sym("a", live_enabled=True))
    client.post("/api/strategies/a/capital", json={"action": "invest", "amount": "10000"}, headers=H())
    client.post("/api/strategies/a/run", json={"mode": "live", "confirm": "a"}, headers=H())
    client.post("/api/strategies/cash", json={"ids": ["a"], "confirm": "CASH"}, headers=H())
    d = client.get("/api/dashboard", headers=H()).json()
    row = d["strategies"][0]
    assert row["positions"] == 0 and row["target_positions"] == 0 and row["last_run"]["target"] == "cash"
    assert d["rollups"]["incubation"]["tickers"] == [] and d["rollups"]["incubation"]["total_target"] == 0
    assert d["rollups"]["incubation"]["unallocated"] == pytest.approx(d["rollups"]["incubation"]["total_capital"])


# ── live (out-of-sample) stats per funded row ─────────────────────────────
def _go_live(sid, d0, holdings, contributed="10000", cash="0"):
    import pandas as pd

    ts = (d0 + pd.Timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store._append(
        store.snapshots_log(),
        {"ts": ts, "strategy": sid, "broker": "paper", "event": "run", "cash": cash,
         "holdings": {k: str(v) for k, v in holdings.items()}, "contributed": contributed},
    )  # fmt: skip
    store._append(
        store.runs_log(), {"ts": ts, "strategy": sid, "mode": "live", "broker": "paper", "status": "executed"}
    )


def test_live_stats_match_backtest_metrics_on_the_sleeve_index():
    import numpy as np

    from msts_trader.symphony import backtest, performance
    from tests.test_studio import _closes

    store.save(make_sym(live_enabled=True))
    closes = _closes(["SPY", "TLT"])
    d0 = closes.index[-120]
    _go_live("momo", d0, {"SPY": 6000 / closes.loc[d0, "SPY"], "TLT": 4000 / closes.loc[d0, "TLT"]})
    r = performance.live_stats("momo", "paper", closes)
    assert r["since"] == d0.date().isoformat() and r["days"] == 120
    nav = 6000 / closes.loc[d0, "SPY"] * closes["SPY"][d0:] + 4000 / closes.loc[d0, "TLT"] * closes["TLT"][d0:]
    m = backtest.metrics(nav.to_numpy() / 10000, list(nav.index))
    for k in ("cagr", "sharpe", "max_drawdown", "total_return"):
        assert r[k] == pytest.approx(m[k], rel=1e-9), k
    assert r["max_drawdown"] > 0 and np.isfinite(r["sharpe"])


def test_live_stats_short_history_skips_annualised_numbers():
    from msts_trader.symphony import performance
    from tests.test_studio import _closes

    store.save(make_sym(live_enabled=True))
    closes = _closes(["SPY"])
    d0 = closes.index[-10]
    _go_live("momo", d0, {"SPY": 10000 / closes.loc[d0, "SPY"]})
    r = performance.live_stats("momo", "paper", closes)
    assert r["cagr"] is None and r["sharpe"] is None  # 9 days annualised would be noise
    assert r["total_return"] == pytest.approx(closes["SPY"].iloc[-1] / closes.loc[d0, "SPY"] - 1)
    assert performance.live_stats("other", "paper", closes) is None  # never went live


def test_live_stats_strip_deposits_from_returns():
    import pandas as pd

    from msts_trader.symphony import performance

    idx = pd.date_range("2026-01-01", periods=60, freq="D")
    closes = pd.DataFrame({"SPY": [100.0] * 60}, index=idx)  # flat price: zero performance
    store.save(make_sym(live_enabled=True))
    _go_live("momo", idx[0], {"SPY": 100})
    # doubled capital on day 30 (all cash): NAV jumps, return must not
    _go_live("momo", idx[30], {"SPY": 100}, contributed="20000", cash="10000")
    r = performance.live_stats("momo", "paper", closes)
    assert r["total_return"] == pytest.approx(0.0) and r["max_drawdown"] == pytest.approx(0.0)


def test_dashboard_live_columns_use_the_price_cache_only(client, tmp_path, monkeypatch):
    import pandas as pd

    from msts_trader.symphony import prices
    from tests.test_studio import _closes

    monkeypatch.setenv("MSTS_PRICES_DIR", str(tmp_path / "prices"))
    monkeypatch.setattr(prices, "fetch", lambda *a, **k: pytest.fail("dashboard must not hit the network"))
    store.save(make_sym(live_enabled=True))
    store.save(make_sym("idle"))
    closes = _closes(["SPY", "TLT"])
    d0 = closes.index[-60]
    _go_live("momo", d0, {"SPY": 50, "TLT": 40})
    # no cache yet -> a reason, not numbers
    rows = {r["id"]: r for r in client.get("/api/dashboard", headers=H()).json()["strategies"]}
    assert rows["idle"]["live"] is None
    assert (
        rows["momo"]["live"]["since"] == d0.date().isoformat() and "no cached prices" in rows["momo"]["live"]["reason"]
    )
    tomorrow = (pd.Timestamp(closes.index[-1]) + pd.Timedelta(days=1)).date()
    for t in ("SPY", "TLT"):
        prices._write_cache(t, closes[t], tomorrow)
    live = {r["id"]: r for r in client.get("/api/dashboard", headers=H()).json()["strategies"]}["momo"]["live"]
    assert live["days"] == 60 and live["cagr"] is not None and live["sharpe"] is not None
    assert 0 <= live["max_drawdown"] < 1 and live["asof"] == closes.index[-1].date().isoformat()


def _config_leftovers(home):
    # what Ron's config.toml can carry from CLI use: whole shares + a min weight
    (home / ".msts-trader" / "config.toml").write_text("whole_shares = true\nmin_weight = 0.5\n", encoding="utf-8")


def test_config_whole_shares_and_min_weight_do_not_leak_into_studio_runs(client):
    import subprocess

    from msts_trader.symphony import runner

    home = Path(os.environ["USERPROFILE"])  # the autouse fixture's temp home
    _config_leftovers(home)
    s = make_sym()  # SPY 60% / TLT 40%, market orders; paper SPY $500, TLT $90
    store.save(s)
    client.post("/api/strategies/momo/capital", json={"action": "invest", "amount": "400"}, headers=H())
    # The bare CLI honours config.toml: SPY's $240 < one $500 share -> 0; TLT 40% < min_weight
    cmd = [a for a in runner.rebalance_cmd(s, "x", mode="dry") if a not in ("--fractional", "--min-weight", "0")]
    csv = home / "t.csv"
    csv.write_text("ticker,weight\nSPY,0.6\nTLT,0.4\n", encoding="utf-8")
    cmd[cmd.index("x")] = str(csv)
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=runner._subprocess_env())
    pv = next(p for p in runner._parse_json_lines(out.stdout) if "orders" in p)
    assert pv["orders"] == [] and pv["sizing"]["whole_shares"] is True
    assert {x["ticker"]: x["note"] for x in pv["not_bought"]} == {
        "SPY": "qty rounds to 0 (whole-share)",
        "TLT": "below min weight 0.5 — ignored",
    }
    # A Studio run of a Market strategy uses the strategy's settings: fractional, no min weight
    res = runner.run(s, mode="dry")
    assert {o["ticker"]: o["quantity"] for o in res["preview"]["orders"]} == {"SPY": "0.48", "TLT": "1.77"}
    assert res["preview"]["sizing"]["whole_shares"] is False and res["preview"]["not_bought"] == []


def test_dashboard_reports_targets_the_engine_did_not_buy(client):
    store.save(make_sym())
    client.post("/api/strategies/momo/capital", json={"action": "invest", "amount": "400"}, headers=H())
    nb = [{"ticker": "SPY", "target_pct": "0.6", "note": "qty rounds to 0 (whole-share)"}]
    store._append(store.runs_log(), {"ts": "2026-01-06T20:00:00Z", "strategy": "momo", "mode": "dry",
                                     "status": "preview", "weights": {"SPY": 0.6, "TLT": 0.4},
                                     "preview": {"orders": [], "not_bought": nb,
                                                 "sizing": {"whole_shares": True, "moc": True}}})  # fmt: skip
    row = client.get("/api/dashboard", headers=H()).json()["strategies"][0]
    assert row["not_bought"] == nb and row["sizing"]["whole_shares"] is True
    store._append(store.runs_log(), {"ts": "2026-01-07T20:00:00Z", "strategy": "momo", "mode": "dry",
                                     "status": "preview", "weights": {"SPY": 1.0}, "preview": {"orders": []}})  # fmt: skip
    row = client.get("/api/dashboard", headers=H()).json()["strategies"][0]
    assert row["not_bought"] is None  # a run from an older version: no data, no guess


def test_nav_series_marks_a_ticker_missing_todays_bar_at_its_last_close():
    import pandas as pd

    from msts_trader.symphony import performance

    idx = pd.date_range("2026-10-05", periods=3, freq="D")
    closes = pd.DataFrame({"SPY": [100.0, 101.0, 102.0], "DECO": [10.0, 11.0, float("nan")]}, index=idx)
    snaps = [{"ts": "2026-10-05T20:00:00Z", "cash": "0", "holdings": {"SPY": "1", "DECO": "10"}, "contributed": "200"}]
    nav = performance.nav_series(snaps, closes, idx[0].date())
    assert list(nav["nav"]) == [200.0, 211.0, 212.0]  # DECO marked at 11 on the 7th, no "no close" error


# ── Home tabs: Live (real money) vs Incubation (paper), pinned tag tabs ────
def _fund_ledger(broker, sid, amount):
    from decimal import Decimal

    from msts_trader import sleeves

    led = sleeves.load(broker, "acct1")
    led.cash[sid] = Decimal(amount)
    led.contributed[sid] = Decimal(amount)
    sleeves.save(led)


def test_stage_splits_real_money_from_paper_and_rollups_follow(client):
    store.save(make_sym("real", broker="alpaca", live_enabled=True))
    store.save(make_sym("sandbox", broker="alpaca", live_enabled=True, paper_account=True))
    store.save(make_sym("preview", broker="alpaca", live_enabled=False))
    store.save(make_sym("paper", live_enabled=True))
    for sid, broker in (("real", "alpaca"), ("sandbox", "alpaca"), ("preview", "alpaca"), ("paper", "paper")):
        _fund_ledger(broker, sid, "1000")
    d = client.get("/api/dashboard", headers=H()).json()
    stage = {r["id"]: r["stage"] for r in d["strategies"]}
    assert stage == {"real": "live", "sandbox": "incubation", "preview": "incubation", "paper": "incubation"}
    assert d["rollups"]["live"]["strategies"] == 1 and d["rollups"]["live"]["total_capital"] == pytest.approx(1000)
    assert d["rollups"]["incubation"]["strategies"] == 3


def test_home_tabs_setting_normalises_and_round_trips(client):
    r = client.put(
        "/api/settings",
        json={"home_tabs": ["  Options ", "options", "Active  strategies", "", "Passive weights"]},
        headers=H(),
    )
    assert r.status_code == 200 and r.json()["home_tabs"] == ["Options", "Active strategies", "Passive weights"]
    assert client.get("/api/dashboard", headers=H()).json()["settings"]["home_tabs"] == r.json()["home_tabs"]
    client.put("/api/settings", json={"weekly_digest": True}, headers=H())  # other settings leave tabs alone
    assert client.get("/api/settings", headers=H()).json()["home_tabs"] == [
        "Options",
        "Active strategies",
        "Passive weights",
    ]
    assert client.put("/api/settings", json={"home_tabs": []}, headers=H()).json()["home_tabs"] == []


# ── Value needs a price for every holding; live runs keep the cache warm ──
def test_dashboard_value_is_unknown_not_cash_when_a_holding_is_unpriced(client, tmp_path, monkeypatch):
    from decimal import Decimal

    import pandas as pd

    from msts_trader import sleeves
    from msts_trader.symphony import prices

    monkeypatch.setenv("MSTS_PRICES_DIR", str(tmp_path / "prices"))
    store.save(make_sym(live_enabled=True))
    led = sleeves.load("paper", "acct1")
    led.cash["momo"] = Decimal("-404")  # a 108%-gross book: the sleeve borrowed
    led.contributed["momo"] = Decimal("10000")
    led.sleeves["momo"] = {"SPY": Decimal("10"), "ETHA": Decimal("100")}
    sleeves.save(led)
    idx = pd.date_range("2026-01-01", periods=3, freq="D")
    prices._write_cache("SPY", pd.Series(500.0, index=idx), (idx[-1] + pd.Timedelta(days=1)).date())
    row = client.get("/api/dashboard", headers=H()).json()["strategies"][0]
    assert row["nav"] is None and row["unpriced"] == ["ETHA"]  # not "-$404"
    prices._write_cache("ETHA", pd.Series(55.0, index=idx), (idx[-1] + pd.Timedelta(days=1)).date())
    row = client.get("/api/dashboard", headers=H()).json()["strategies"][0]
    assert row["unpriced"] == [] and row["nav"] == pytest.approx(-404 + 10 * 500 + 100 * 55)


def test_live_run_refreshes_prices_for_what_the_sleeve_holds(client, monkeypatch):
    from msts_trader.symphony import prices, runner
    from tests.test_studio import _closes

    calls = []
    monkeypatch.setattr(
        prices, "load_closes", lambda tickers, start=None, **k: calls.append(sorted(tickers)) or _closes(tickers)
    )
    s = make_sym(live_enabled=True)
    store.save(s)
    client.post("/api/strategies/momo/capital", json={"action": "invest", "amount": "10000"}, headers=H())
    res = runner.run(s, mode="live")
    assert res["status"] == "executed"
    assert calls[-1] == ["SPY", "TLT"]  # the holdings, after the fills
    n = len(calls)
    runner.run(s, mode="dry")
    assert len(calls) == n + 1  # a dry run only evaluates; no extra refresh


def test_price_refresh_failure_never_fails_the_run(client, monkeypatch):
    from msts_trader.symphony import prices, runner
    from tests.test_studio import _closes

    state = {"n": 0}

    def flaky(tickers, start=None, **k):
        state["n"] += 1
        if state["n"] > 1:  # evaluation works, the post-run refresh blows up
            raise prices.PriceError("yahoo is down")
        return _closes(tickers)

    monkeypatch.setattr(prices, "load_closes", flaky)
    s = make_sym(live_enabled=True)
    store.save(s)
    client.post("/api/strategies/momo/capital", json={"action": "invest", "amount": "10000"}, headers=H())
    assert runner.run(s, mode="live")["status"] == "executed"
