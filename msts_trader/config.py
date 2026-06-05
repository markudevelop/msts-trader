"""Optional config file for defaults: ~/.msts-trader/config.toml (or --config).

Lets you set broker / threshold / csv source / safety limits / notify URL
once instead of passing them on every command. Resolution order for any
value is: explicit CLI flag > environment > config file > built-in default.

Example ~/.msts-trader/config.toml:

    broker = "tastytrade"
    threshold = 0.04
    csv_url = "https://example.com/weights.csv"
    max_notional = 60000
    max_stale_hours = 36
    notify_url = "https://discord.com/api/webhooks/..."
    quiet = false
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore

DEFAULT_PATH = Path(os.path.expanduser("~/.msts-trader/config.toml"))

_KNOWN = {
    "broker", "threshold", "csv_file", "csv_url", "creds_file",
    "max_notional", "max_stale_hours", "notify_url", "quiet",
    "account",  # array of [[account]] tables for the `multi` command
}


class ConfigError(ValueError):
    pass


def load(path: str | Path | None = None) -> dict:
    """Load the config file if it exists. Returns {} when absent.

    Raises ConfigError on a malformed file the user explicitly pointed at.
    """
    explicit = path is not None
    p = Path(path) if path else DEFAULT_PATH
    if not p.exists():
        if explicit:
            raise ConfigError(f"config file not found: {p}")
        return {}
    if tomllib is None:  # pragma: no cover
        raise ConfigError("TOML support requires Python 3.11+")
    try:
        with p.open("rb") as f:
            data = tomllib.load(f)
    except Exception as e:
        raise ConfigError(f"invalid TOML in {p}: {e}") from e
    unknown = set(data) - _KNOWN
    if unknown:
        raise ConfigError(f"unknown config keys: {sorted(unknown)} — allowed: {sorted(_KNOWN)}")
    return data


def pick(cli_value, config: dict, key: str, default=None):
    """Resolve a single setting: CLI value wins, else config, else default."""
    if cli_value is not None:
        return cli_value
    if key in config and config[key] is not None:
        return config[key]
    return default
