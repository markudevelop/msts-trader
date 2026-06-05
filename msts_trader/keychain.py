from __future__ import annotations

import json

import keyring

SERVICE = "msts-trader"
KEY = "tastytrade"


class CredsMissingError(RuntimeError):
    pass


def save_creds(provider_secret: str, refresh_token: str, account_id: str | None) -> None:
    blob = json.dumps(
        {
            "provider_secret": provider_secret,
            "refresh_token": refresh_token,
            "account_id": account_id or "",
        }
    )
    keyring.set_password(SERVICE, KEY, blob)


def load_creds() -> tuple[str, str, str | None]:
    raw = keyring.get_password(SERVICE, KEY)
    if not raw:
        raise CredsMissingError("no creds — run `msts-trader login` first")
    d = json.loads(raw)
    return d["provider_secret"], d["refresh_token"], d.get("account_id") or None


def clear_creds() -> None:
    try:
        keyring.delete_password(SERVICE, KEY)
    except keyring.errors.PasswordDeleteError:
        pass
