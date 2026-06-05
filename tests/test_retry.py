from __future__ import annotations

import pytest

from msts_trader.retry import is_transient, with_retry


def test_is_transient_true():
    assert is_transient(Exception("HTTP 429 Too Many Requests"))
    assert is_transient(Exception("connection reset by peer"))
    assert is_transient(Exception("request timed out"))


def test_is_transient_false():
    assert not is_transient(Exception("invalid_grant: Grant revoked"))
    assert not is_transient(Exception("insufficient buying power"))


def test_with_retry_succeeds_first_try():
    calls = []
    assert with_retry(lambda: calls.append(1) or "ok", sleep=lambda s: None) == "ok"
    assert len(calls) == 1


def test_with_retry_retries_transient_then_succeeds():
    state = {"n": 0}

    def fn():
        state["n"] += 1
        if state["n"] < 3:
            raise Exception("429 rate limit")
        return "done"

    assert with_retry(fn, attempts=3, sleep=lambda s: None) == "done"
    assert state["n"] == 3


def test_with_retry_reraises_non_transient_immediately():
    state = {"n": 0}

    def fn():
        state["n"] += 1
        raise ValueError("bad creds")

    with pytest.raises(ValueError):
        with_retry(fn, attempts=3, sleep=lambda s: None)
    assert state["n"] == 1  # no retries for non-transient


def test_with_retry_gives_up_after_attempts():
    with pytest.raises(Exception, match="timeout"):
        with_retry(lambda: (_ for _ in ()).throw(Exception("timeout")), attempts=2, sleep=lambda s: None)
