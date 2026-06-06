"""Robust input prompts that don't break in VS Code / Cursor integrated terminals.

`rich.prompt.Prompt.ask(password=True)` and `getpass.getpass()` both fail
silently inside VS Code's integrated terminal (and Cursor's, which is a
fork): the prompt is shown but stdin is never read, so the user can't
type or paste anything. This module wraps the prompt layer so that:

  - the hidden-input path is tried first (clean UX in a real terminal),
  - if it fails or returns nothing, we fall back to a visible read,
  - if the runtime is non-interactive (CI, piped input), we read a single
    line from stdin without trying to clear the screen.

Detection cribbed from common workarounds for the known Python +
VS Code getpass issue.
"""
from __future__ import annotations

import os
import sys
from typing import Optional


def _is_vscode_like() -> bool:
    return any(
        os.environ.get(k, "").startswith(v)
        for k, v in (
            ("TERM_PROGRAM", "vscode"),
            ("TERM_PROGRAM", "cursor"),
            ("VSCODE_INJECTION", "1"),
        )
    )


def _is_hidden_input_flaky() -> bool:
    """Terminals where getpass / hidden input is known to drop paste & typing.

    Covers VS Code / Cursor integrated terminals and Windows Terminal
    (WT_SESSION), plus any Windows console (getpass on Windows reads via
    msvcrt and silently swallows pasted input in several configurations).
    """
    if _is_vscode_like():
        return True
    if os.environ.get("WT_SESSION"):
        return True
    if sys.platform.startswith("win"):
        return True
    return False


def _is_interactive() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def _read_line() -> str:
    return sys.stdin.readline().rstrip("\n").rstrip("\r")


def ask_text(prompt: str, default: Optional[str] = None, allow_blank: bool = True) -> str:
    """Visible prompt. Echoes what the user types/pastes."""
    suffix = f" [{default}]" if default else ""
    while True:
        sys.stdout.write(f"{prompt}{suffix}: ")
        sys.stdout.flush()
        try:
            line = input().strip()
        except EOFError:
            line = ""
        if not line and default is not None:
            return default
        if line or allow_blank:
            return line


def strip_quotes(value: str) -> str:
    """Strip a single matching pair of surrounding quotes.

    Windows `set VAR="x"` (cmd) captures the quotes into the value, and
    creds files often quote values too. We don't want quotes inside a
    secret, so peel one matching pair.
    """
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def env_value(name: str) -> Optional[str]:
    """Read an env var, strip surrounding quotes and whitespace.

    Returns None for missing or whitespace-only values.
    """
    raw = os.environ.get(name)
    if raw is None:
        return None
    cleaned = strip_quotes(raw)
    return cleaned or None


def ask_secret(prompt: str, *, env_var: Optional[str] = None) -> str:
    """Hidden prompt with safe fallback.

    If `env_var` is set in the environment, use it directly (no prompt).
    Otherwise try `getpass.getpass`; if that returns empty (the typical
    VS Code / Cursor / Windows-Terminal failure mode), retry with a
    visible prompt and warn the user that the input will be displayed.
    """
    if env_var:
        val = env_value(env_var)
        if val:
            # Announce env-sourced values so a stale exported secret (e.g. a
            # revoked refresh token) can't silently masquerade as fresh input.
            sys.stderr.write(
                f"\n[notice] using {env_var} from the environment "
                f"(skipping prompt). unset it to be prompted instead.\n"
            )
            sys.stderr.flush()
            return val

    # Non-interactive: just read a line from stdin.
    if not _is_interactive():
        sys.stdout.write(f"{prompt}: ")
        sys.stdout.flush()
        return _read_line()

    # On terminals where hidden input is known to drop paste/typing
    # (VS Code, Cursor, Windows Terminal, any Windows console), skip the
    # dead getpass prompt entirely and go straight to visible input so the
    # user isn't left staring at an unresponsive cursor.
    if _is_hidden_input_flaky():
        sys.stderr.write(
            "\n[notice] this terminal doesn't reliably accept hidden/pasted "
            "input, so the value will be shown as you type or paste it. "
            "(Use --creds-file to avoid typing secrets entirely — see the "
            "README.)\n"
        )
        sys.stderr.flush()
        return ask_text(f"{prompt} (visible)", allow_blank=False)

    import getpass  # imported lazily so non-interactive paths don't pay for it

    try:
        val = getpass.getpass(f"{prompt}: ")
    except (EOFError, KeyboardInterrupt):
        raise
    except Exception:
        val = ""

    if val:
        return val

    # Empty input is the VS Code failure mode. Retry visibly with a warning.
    if _is_vscode_like():
        sys.stderr.write(
            "\n[warning] hidden input is not supported in VS Code / Cursor's "
            "integrated terminal — falling back to visible input. Your secret "
            "will be displayed as you paste.\n"
        )
    else:
        sys.stderr.write(
            "\n[warning] received no input from the hidden prompt — falling "
            "back to visible input. Your secret will be displayed.\n"
        )
    sys.stderr.flush()
    sys.stdout.write(f"{prompt} (visible): ")
    sys.stdout.flush()
    return _read_line()


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    sys.stdout.write(f"{prompt} {suffix}: ")
    sys.stdout.flush()
    try:
        line = input().strip().lower()
    except EOFError:
        return default
    if not line:
        return default
    return line in {"y", "yes", "1", "true"}
