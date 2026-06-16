from __future__ import annotations

import pytest

from msts_trader import config


def test_load_absent_default_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DEFAULT_PATH", tmp_path / "nope.toml")
    assert config.load() == {}


def test_load_explicit_missing_raises():
    with pytest.raises(config.ConfigError, match="not found"):
        config.load("/no/such/config.toml")


def test_load_valid(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('broker = "alpaca"\nthreshold = 0.02\nmax_notional = 60000\n')
    cfg = config.load(p)
    assert cfg["broker"] == "alpaca"
    assert cfg["threshold"] == 0.02
    assert cfg["max_notional"] == 60000


def test_load_accepts_whole_shares_and_telegram(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        'whole_shares = true\n'
        'telegram_token = "123:abc"\n'
        'telegram_chat_id = "987"\n'
    )
    cfg = config.load(p)
    assert cfg["whole_shares"] is True
    assert cfg["telegram_token"] == "123:abc"
    assert cfg["telegram_chat_id"] == "987"


def test_load_unknown_key_raises(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('bogus = 1\n')
    with pytest.raises(config.ConfigError, match="unknown config keys"):
        config.load(p)


def test_load_bad_toml_raises(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('broker = "unterminated\n')
    with pytest.raises(config.ConfigError, match="invalid TOML"):
        config.load(p)


def test_pick_cli_wins():
    assert config.pick("cli", {"broker": "cfg"}, "broker", "def") == "cli"


def test_pick_config_when_no_cli():
    assert config.pick(None, {"broker": "cfg"}, "broker", "def") == "cfg"


def test_pick_default_when_neither():
    assert config.pick(None, {}, "broker", "def") == "def"
