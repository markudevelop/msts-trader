from __future__ import annotations

from msts_trader.prompts import env_value, strip_quotes


def test_strip_double_quotes():
    assert strip_quotes('"abc"') == "abc"


def test_strip_single_quotes():
    assert strip_quotes("'abc'") == "abc"


def test_strip_no_quotes():
    assert strip_quotes("abc") == "abc"


def test_strip_mismatched_quotes_left_alone():
    assert strip_quotes("'abc\"") == "'abc\""


def test_strip_whitespace():
    assert strip_quotes('  "abc"  ') == "abc"


def test_env_value_strips_quotes(monkeypatch):
    monkeypatch.setenv("MSTS_X", '"quoted"')
    assert env_value("MSTS_X") == "quoted"


def test_env_value_missing_returns_none(monkeypatch):
    monkeypatch.delenv("MSTS_MISSING", raising=False)
    assert env_value("MSTS_MISSING") is None


def test_env_value_whitespace_only_returns_none(monkeypatch):
    monkeypatch.setenv("MSTS_BLANK", "   ")
    assert env_value("MSTS_BLANK") is None
