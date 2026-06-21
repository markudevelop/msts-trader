"""OS-keychain credential storage, keyed by broker name.

Stores `creds:<broker>` JSON blobs plus a `default_broker` pointer.
Adding a new broker = add login flow in __main__.py; this module
already supports arbitrary keys.
"""

from __future__ import annotations

import json

import keyring

SERVICE = "msts-trader"
DEFAULT_KEY = "default_broker"


class CredsMissingError(RuntimeError):
    pass


def save(broker: str, payload: dict) -> None:
    keyring.set_password(SERVICE, f"creds:{broker}", json.dumps(payload))


def load(broker: str) -> dict:
    raw = keyring.get_password(SERVICE, f"creds:{broker}")
    if not raw:
        raise CredsMissingError(
            f"no stored creds for broker {broker!r} — run `msts-trader login --broker {broker}` first"
        )
    return json.loads(raw)


def clear(broker: str) -> None:
    try:
        keyring.delete_password(SERVICE, f"creds:{broker}")
    except keyring.errors.PasswordDeleteError:
        pass


def set_default(broker: str) -> None:
    keyring.set_password(SERVICE, DEFAULT_KEY, broker)


def get_default() -> str | None:
    return keyring.get_password(SERVICE, DEFAULT_KEY)


def clear_default() -> None:
    try:
        keyring.delete_password(SERVICE, DEFAULT_KEY)
    except keyring.errors.PasswordDeleteError:
        pass


def list_brokers() -> list[str]:
    """Best-effort list of brokers with creds in this keychain.

    Some keyring backends don't expose enumeration; fall back to probing
    the known supported names.
    """
    from .brokers import SUPPORTED

    out: list[str] = []
    for name in SUPPORTED:
        try:
            if keyring.get_password(SERVICE, f"creds:{name}"):
                out.append(name)
        except Exception:
            continue
    return out
