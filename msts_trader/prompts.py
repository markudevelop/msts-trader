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


def ask_secret(prompt: str, *, env_var: Optional[str] = None) -> str:
    """Hidden prompt with safe fallback.

    If `env_var` is set in the environment, use it directly (no prompt).
    Otherwise try `getpass.getpass`; if that returns empty (the typical
    VS Code / Cursor failure mode), retry with a visible prompt and warn
    the user that the input will be displayed.
    """
    if env_var:
        val = os.environ.get(env_var)
        if val:
            return val

    # Non-interactive: just read a line from stdin.
    if not _is_interactive():
        sys.stdout.write(f"{prompt}: ")
        sys.stdout.flush()
        return _read_line()

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
