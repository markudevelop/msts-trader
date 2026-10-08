"""Studio's Sleeve tools panel: `msts-trader sleeve` admin commands over the API,
run for real against the paper broker."""

from __future__ import annotations

from msts_trader import sleeves
from msts_trader.symphony import store
from tests.test_studio import H, client, make_sym, studio_home  # noqa: F401  (fixtures)


def _tool(c, **body):
    return c.post("/api/strategies/momo/sleeve-tool", json=body, headers=H())


def _funded(c):
    store.save(make_sym(live_enabled=True))
    assert (
        c.post("/api/strategies/momo/capital", json={"action": "invest", "amount": "10000"}, headers=H()).status_code
        == 200
    )
    live = c.post("/api/strategies/momo/run", json={"mode": "live", "confirm": "momo"}, headers=H()).json()
    assert live["status"] == "executed", live
    return sleeves.load("paper", "PAPER").tally("momo", "SPY")


def test_release_adopt_adjust_move_the_tally_without_trading(client):  # noqa: F811
    spy = _funded(client)
    r = _tool(client, action="release", ticker="spy", qty="1")
    assert r.status_code == 200 and r.json()["ok"], r.text
    assert sleeves.load("paper", "PAPER").tally("momo", "SPY") == spy - 1

    r = _tool(client, action="adopt", ticker="SPY", qty="1")
    assert r.json()["ok"], r.json()["output"]
    assert sleeves.load("paper", "PAPER").tally("momo", "SPY") == spy

    r = _tool(client, action="adjust", ticker="TLT", qty="0")
    assert r.json()["ok"], r.json()["output"]
    assert "TLT" not in r.json()["sleeve"][0]["holdings"]


def test_reconcile_reports_negative_residual_as_data(client):  # noqa: F811
    _funded(client)
    ok = _tool(client, action="reconcile").json()
    assert ok["ok"] and "consistent" in ok["output"]

    _tool(client, action="adjust", ticker="SPY", qty="1000000")
    bad = _tool(client, action="reconcile")
    assert bad.status_code == 200  # the table is the point, not an HTTP error
    assert not bad.json()["ok"] and "claim more" in bad.json()["output"]


def test_base_and_cap_round_trip_into_the_ledger_policy(client):  # noqa: F811
    _funded(client)
    assert _tool(client, action="base", spec="20%").json()["ok"]
    assert _tool(client, action="cap", spec="$50,000").json()["ok"]
    policy = client.get("/api/strategies/momo/sleeve", headers=H()).json()["ledgers"][0]["policy"]
    assert policy["base"] == {"mode": "pct-nav", "value": "0.2"}
    assert policy["cap"] == {"mode": "fixed", "value": "50000"}

    assert _tool(client, action="base", spec="own-nav").json()["ok"]
    assert _tool(client, action="cap", spec="off").json()["ok"]
    assert client.get("/api/strategies/momo/sleeve", headers=H()).json()["ledgers"][0]["policy"] == {}


def test_inputs_are_validated_before_reaching_the_cli(client):  # noqa: F811
    store.save(make_sym())
    for body in (
        {"action": "adopt", "ticker": "-x", "qty": "1"},  # would parse as an option
        {"action": "adopt", "ticker": "SPY", "qty": "abc"},
        {"action": "release", "ticker": "SPY", "qty": "0"},  # 0 only for set tally
        {"action": "base", "spec": "off"},  # off is a cap value
        {"action": "cap", "spec": "--help"},
        {"action": "invest"},  # not a sleeve tool
    ):
        assert _tool(client, **body).status_code == 422, body
