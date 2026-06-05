"""Tests for the robust prompt helpers that work around the
VS Code / Cursor integrated-terminal `getpass` bug.
"""
from __future__ import annotations

import io
import sys

import pytest

from msts_trader import prompts


def test_ask_secret_reads_env_var(monkeypatch):
    monkeypatch.setenv("MSTS_TEST_SECRET", "from-env")
    assert prompts.ask_secret("ignored", env_var="MSTS_TEST_SECRET") == "from-env"


def test_ask_secret_env_var_overrides_prompt(monkeypatch):
    monkeypatch.setenv("MSTS_TEST_SECRET", "env-value")
    # If env var is set, getpass should NEVER be called
    monkeypatch.setattr(
        "getpass.getpass",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("getpass should not run")),
    )
    assert prompts.ask_secret("p", env_var="MSTS_TEST_SECRET") == "env-value"


def test_ask_secret_falls_back_to_visible_when_getpass_empty(monkeypatch, capsys):
    # Pretend we are interactive but getpass returns empty (the VS Code failure mode)
    monkeypatch.setattr(prompts, "_is_interactive", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda *a, **kw: "")
    monkeypatch.setattr(sys, "stdin", io.StringIO("typed-secret\n"))
    val = prompts.ask_secret("provider secret")
    assert val == "typed-secret"
    err = capsys.readouterr().err
    assert "visible" in err.lower()


def test_ask_secret_falls_back_when_getpass_raises(monkeypatch, capsys):
    monkeypatch.setattr(prompts, "_is_interactive", lambda: True)

    def raise_io(*a, **kw):
        raise OSError("no tty")

    monkeypatch.setattr("getpass.getpass", raise_io)
    monkeypatch.setattr(sys, "stdin", io.StringIO("fallback-typed\n"))
    val = prompts.ask_secret("p")
    assert val == "fallback-typed"


def test_ask_secret_uses_getpass_when_it_works(monkeypatch):
    monkeypatch.setattr(prompts, "_is_interactive", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda *a, **kw: "hidden-value")
    val = prompts.ask_secret("p")
    assert val == "hidden-value"


def test_ask_secret_non_interactive_reads_stdin(monkeypatch):
    monkeypatch.setattr(prompts, "_is_interactive", lambda: False)
    monkeypatch.setattr(sys, "stdin", io.StringIO("piped\n"))
    assert prompts.ask_secret("p") == "piped"


def test_ask_text_uses_default_when_blank(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "")
    assert prompts.ask_text("name", default="bob") == "bob"


def test_ask_text_returns_typed(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "alice")
    assert prompts.ask_text("name") == "alice"


@pytest.mark.parametrize("ans, default, expected", [
    ("y", False, True),
    ("yes", False, True),
    ("n", True, False),
    ("", True, True),
    ("", False, False),
])
def test_ask_yes_no(monkeypatch, ans, default, expected):
    monkeypatch.setattr("builtins.input", lambda *a, **kw: ans)
    assert prompts.ask_yes_no("ok?", default=default) is expected


def test_vscode_detection(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "vscode")
    assert prompts._is_vscode_like() is True
    monkeypatch.setenv("TERM_PROGRAM", "cursor")
    assert prompts._is_vscode_like() is True
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    monkeypatch.delenv("VSCODE_INJECTION", raising=False)
    assert prompts._is_vscode_like() is False
