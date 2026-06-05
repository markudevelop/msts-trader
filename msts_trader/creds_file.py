"""Load broker credentials from a file so secrets never go through a prompt.

Supports two formats, auto-detected:

  1. JSON   — {"TT_PROVIDER_SECRET": "...", "TT_REFRESH_TOKEN": "..."}
  2. dotenv — KEY=VALUE lines, `#` comments, blank lines ignored

Both use the same key names as the environment variables documented in
the README (TT_PROVIDER_SECRET, APCA_API_KEY_ID, etc.). Loaded values are
pushed into os.environ (without overwriting anything already set there),
so the existing env-var-aware login path picks them up unchanged.

Lowercase kwarg aliases are also accepted and mapped to the canonical
ENV names, so a file like {"provider_secret": "..."} works too.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .prompts import strip_quotes

# Map friendly / lowercase keys a user might write to the canonical env names.
_ALIASES = {
    "provider_secret": "TT_PROVIDER_SECRET",
    "refresh_token": "TT_REFRESH_TOKEN",
    "account_id": "TT_ACCOUNT_ID",
    "api_key": "APCA_API_KEY_ID",
    "api_key_id": "APCA_API_KEY_ID",
    "secret_key": "APCA_API_SECRET_KEY",
    "paper": "APCA_PAPER",
    "app_key": "SCHWAB_APP_KEY",
    "app_secret": "SCHWAB_APP_SECRET",
    "callback_url": "SCHWAB_CALLBACK_URL",
    "host": "IBKR_HOST",
    "port": "IBKR_PORT",
    "client_id": "IBKR_CLIENT_ID",
    "starting_cash": "PAPER_STARTING_CASH",
}


class CredsFileError(ValueError):
    pass


def _normalize_key(key: str) -> str:
    k = key.strip()
    return _ALIASES.get(k.lower(), k)


def parse(text: str) -> dict[str, str]:
    """Parse JSON or dotenv text into a {ENV_NAME: value} dict."""
    text = text.lstrip("﻿").strip()
    if not text:
        raise CredsFileError("creds file is empty")

    out: dict[str, str] = {}
    if text[0] in "{[":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise CredsFileError(f"invalid JSON: {e}") from e
        if not isinstance(data, dict):
            raise CredsFileError("JSON creds file must be an object")
        for k, v in data.items():
            out[_normalize_key(str(k))] = strip_quotes(str(v))
        return out

    # dotenv-style
    for i, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise CredsFileError(f"line {i}: expected KEY=VALUE, got {line!r}")
        key, val = line.split("=", 1)
        key = key.strip()
        if not key:
            raise CredsFileError(f"line {i}: empty key")
        out[_normalize_key(key)] = strip_quotes(val)
    if not out:
        raise CredsFileError("no key/value pairs found")
    return out


def load_into_env(path: str | Path, *, overwrite: bool = False) -> list[str]:
    """Read a creds file and populate os.environ. Returns the keys set.

    By default, does not clobber variables already present in the
    environment (so an explicit `export FOO=...` still wins).
    """
    p = Path(path)
    if not p.exists():
        raise CredsFileError(f"creds file not found: {p}")
    parsed = parse(p.read_text(encoding="utf-8"))
    set_keys: list[str] = []
    for k, v in parsed.items():
        if overwrite or not os.environ.get(k):
            os.environ[k] = v
            set_keys.append(k)
    return set_keys
