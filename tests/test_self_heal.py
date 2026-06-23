"""Self-heal loop tests for _post_trade_verify (msts_trader.__main__).

The broker round-trip (_verify_once), market clock, executor, and notifier are monkeypatched so
the control flow — re-execute when off-target, stop when converged or when capped or market-closed
— is exercised deterministically without a broker.
"""
import types

from msts_trader import __main__ as m


class _Res:
    def __init__(self, ok):
        self.ok = ok
        self.residual = [] if ok else [object()]

    def summary(self):
        return "converged" if self.ok else "NOT converged"


class _Post:
    def __init__(self, orders):
        self.orders = orders


def _broker():
    return types.SimpleNamespace(name="paper", account_id="ACC")


def _patch(monkeypatch, *, verify_seq, market="open"):
    calls = {"verify": 0, "exec": 0}

    def fake_verify_once(*a, **k):
        i = min(calls["verify"], len(verify_seq) - 1)
        calls["verify"] += 1
        return verify_seq[i]

    monkeypatch.setattr(m, "_verify_once", fake_verify_once)
    monkeypatch.setattr(m, "market_status", lambda: types.SimpleNamespace(status=market))
    monkeypatch.setattr(m, "_execute", lambda *a, **k: calls.__setitem__("exec", calls["exec"] + 1))
    monkeypatch.setattr(m, "_do_notify", lambda *a, **k: None)
    monkeypatch.setattr(m, "say", lambda *a, **k: None)
    return calls


def test_self_heal_executes_until_converged(monkeypatch):
    seq = [(_Res(False), _Post([1, 2])), (_Res(True), _Post([]))]
    calls = _patch(monkeypatch, verify_seq=seq)
    res = m._post_trade_verify(_broker(), [], settle_seconds=0, self_heal=True, heal_passes=1)
    assert res.ok is True
    assert calls["exec"] == 1       # one heal pass executed
    assert calls["verify"] == 2     # verify -> heal -> verify


def test_self_heal_disabled_reports_only(monkeypatch):
    calls = _patch(monkeypatch, verify_seq=[(_Res(False), _Post([1]))])
    res = m._post_trade_verify(_broker(), [], settle_seconds=0, self_heal=False, heal_passes=1)
    assert res.ok is False
    assert calls["exec"] == 0       # report-only, never re-trades


def test_self_heal_skips_when_market_closed(monkeypatch):
    calls = _patch(monkeypatch, verify_seq=[(_Res(False), _Post([1]))], market="closed")
    res = m._post_trade_verify(_broker(), [], settle_seconds=0, self_heal=True, heal_passes=1)
    assert res.ok is False
    assert calls["exec"] == 0       # never re-trades into a closed market


def test_self_heal_caps_passes(monkeypatch):
    # never converges -> must stop at heal_passes, not loop forever
    calls = _patch(monkeypatch, verify_seq=[(_Res(False), _Post([1]))])
    res = m._post_trade_verify(_broker(), [], settle_seconds=0, self_heal=True, heal_passes=2)
    assert res.ok is False
    assert calls["exec"] == 2       # exactly heal_passes executions
    assert calls["verify"] == 3     # initial + after each of 2 heals


def test_converged_first_pass_no_execute(monkeypatch):
    calls = _patch(monkeypatch, verify_seq=[(_Res(True), _Post([]))])
    res = m._post_trade_verify(_broker(), [], settle_seconds=0, self_heal=True, heal_passes=1)
    assert res.ok is True
    assert calls["exec"] == 0       # already converged, nothing to heal


def test_moc_run_never_self_heals(monkeypatch):
    # An MOC order won't fill until the closing auction, so post-trade verify
    # always sees "not converged" right after submission. Self-heal MUST NOT
    # re-execute (it would place an immediate MARKET order on top of the resting
    # MOC — the double-execution bug). Reports only, never re-trades.
    calls = _patch(monkeypatch, verify_seq=[(_Res(False), _Post([1, 2]))], market="open")
    res = m._post_trade_verify(_broker(), [], settle_seconds=0, self_heal=True, heal_passes=2, moc=True)
    assert res.ok is False
    assert calls["exec"] == 0       # MOC: never re-trades despite self_heal=True
    assert calls["verify"] == 1     # single verify, no heal loop
