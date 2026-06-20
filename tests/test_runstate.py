from __future__ import annotations

from decimal import Decimal

import pytest

from msts_trader import runstate
from msts_trader.models import Target


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(runstate, "STATE_PATH", tmp_path / "runstate.json")


def _targets():
    return [Target(ticker="SPY", weight=Decimal("0.5")), Target(ticker="SHV", weight=Decimal("0.5"))]


def test_fingerprint_stable_and_order_independent():
    a = runstate.fingerprint("tastytrade", "ACC", _targets())
    b = runstate.fingerprint("tastytrade", "ACC", list(reversed(_targets())))
    assert a == b


def test_fingerprint_differs_by_account():
    a = runstate.fingerprint("tastytrade", "ACC1", _targets())
    b = runstate.fingerprint("tastytrade", "ACC2", _targets())
    assert a != b


def test_not_done_initially():
    fp = runstate.fingerprint("tastytrade", "ACC", _targets())
    assert runstate.already_done(fp) is False


def test_record_then_done():
    fp = runstate.fingerprint("tastytrade", "ACC", _targets())
    runstate.record(fp)
    assert runstate.already_done(fp) is True


def test_different_targets_not_done():
    fp1 = runstate.fingerprint("tastytrade", "ACC", _targets())
    runstate.record(fp1)
    fp2 = runstate.fingerprint("tastytrade", "ACC", [Target(ticker="QQQ", weight=Decimal("1.0"))])
    assert runstate.already_done(fp2) is False


def test_fingerprint_differs_by_execution_params():
    # Same targets but a materially different plan must NOT dedupe as a duplicate.
    base = runstate.fingerprint("tastytrade", "ACC", _targets(), {"scope": "whole-book", "sweep": True})
    diff_scope = runstate.fingerprint("tastytrade", "ACC", _targets(), {"scope": "per-ticker", "sweep": True})
    diff_sweep = runstate.fingerprint("tastytrade", "ACC", _targets(), {"scope": "whole-book", "sweep": False})
    diff_alloc = runstate.fingerprint("tastytrade", "ACC", _targets(), {"scope": "whole-book", "sweep": True, "allocation": Decimal("50000")})
    assert len({base, diff_scope, diff_sweep, diff_alloc}) == 4


def test_fingerprint_same_params_is_stable():
    p = {"scope": "whole-book", "sweep": True, "allocation": None, "threshold": 0.04}
    assert runstate.fingerprint("tastytrade", "ACC", _targets(), p) == \
        runstate.fingerprint("tastytrade", "ACC", _targets(), dict(p))


def test_fingerprint_no_params_is_backward_compatible():
    # Passing no params hashes identically to the legacy 3-arg form.
    assert runstate.fingerprint("tastytrade", "ACC", _targets()) == \
        runstate.fingerprint("tastytrade", "ACC", _targets(), None)


def test_corrupted_state_file_does_not_crash():
    # A truncated/garbage state file must not raise — treat as "not done".
    runstate.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    runstate.STATE_PATH.write_text("{not valid json")
    fp = runstate.fingerprint("tastytrade", "ACC", _targets())
    assert runstate.already_done(fp) is False
    # record() must also recover and overwrite cleanly.
    runstate.record(fp)
    assert runstate.already_done(fp) is True
