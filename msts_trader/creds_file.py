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

from .prompts import env_value, strip_quotes

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


def broker_kwargs(broker: str, get) -> dict | None:
    """Build `make()` kwargs for a broker from a value getter.

    `get(name)` returns the value for an env-style key (TT_PROVIDER_SECRET,
    APCA_API_KEY_ID, ...) or None. Returns the kwargs dict if the required
    keys are present, else None. The getter abstraction lets us source
    from os.environ (headless) OR from a per-account mapping (multi),
    without leaking one account's secrets into another's.
    """
    e = get
    if broker == "tastytrade":
        ps, rt = e("TT_PROVIDER_SECRET"), e("TT_REFRESH_TOKEN")
        if ps and rt:
            return {"provider_secret": ps, "refresh_token": rt, "account_id": e("TT_ACCOUNT_ID")}
        return None
    if broker == "alpaca":
        k, s = e("APCA_API_KEY_ID"), e("APCA_API_SECRET_KEY")
        if k and s:
            raw = e("APCA_PAPER")
            paper = True if raw is None else raw.lower() in {"1", "true", "yes", "paper"}
            return {"api_key": k, "secret_key": s, "paper": paper}
        return None
    if broker == "ibkr":
        host, port = e("IBKR_HOST"), e("IBKR_PORT")
        if host or port:
            return {
                "host": host or "127.0.0.1",
                "port": int(port or "4002"),
                "client_id": int(e("IBKR_CLIENT_ID") or "17"),
                "account_id": e("IBKR_ACCOUNT_ID"),
            }
        return None
    if broker == "schwab":
        k, s = e("SCHWAB_APP_KEY"), e("SCHWAB_APP_SECRET")
        if k and s:
            return {"app_key": k, "app_secret": s, "callback_url": e("SCHWAB_CALLBACK_URL") or "https://127.0.0.1:8182/"}
        return None
    if broker == "hyperliquid":
        pk = e("HL_PRIVATE_KEY")
        if pk:
            raw = e("HL_TESTNET")
            testnet = bool(raw) and raw.lower() in {"1", "true", "yes"}
            return {"private_key": pk, "account_address": e("HL_ACCOUNT_ADDRESS"), "testnet": testnet}
        return None
    if broker == "paper":
        sc = e("PAPER_STARTING_CASH")
        if sc:
            return {"starting_cash": sc}
        return None
    return None


def broker_kwargs_from_env(broker: str) -> dict | None:
    """Build `make()` kwargs for a broker from environment variables.

    Returns the kwargs dict if the required vars are present, else None
    (so the caller can fall back to the OS keychain). This is what makes
    fully headless runs possible: set the env (or load a --creds-file)
    and rebalance without ever running an interactive `login`.
    """
    return broker_kwargs(broker, env_value)


def broker_kwargs_from_file(broker: str, path: str | Path) -> dict | None:
    """Build `make()` kwargs for one broker from a single creds file.

    Values come from the file first, with env as a fallback for any key
    the file omits. Isolated per call — no os.environ mutation — so
    several accounts can be built in one process without cross-leakage.
    """
    p = Path(os.path.expanduser(str(path)))
    if not p.exists():
        raise CredsFileError(f"creds file not found: {p}")
    parsed = parse(p.read_text(encoding="utf-8"))

    def get(name: str):
        return parsed.get(name) or env_value(name)

    return broker_kwargs(broker, get)


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
