"""Turn raw broker auth exceptions into clear, actionable messages.

The broker SDKs surface low-level errors (e.g. Tastytrade's
`{'error_code': 'invalid_grant', 'error_description': 'Grant revoked'}`)
that don't tell a user what to actually do. This maps the common ones to
plain-language guidance.
"""
from __future__ import annotations


def explain_login_error(broker: str, err: Exception) -> str:
    msg = str(err)
    low = msg.lower()

    if broker == "tastytrade":
        if "invalid_grant" in low or "grant revoked" in low or "revoked" in low:
            return (
                "Your Tastytrade refresh token has been revoked or has expired.\n\n"
                "Refresh tokens are invalidated when you regenerate them, when the "
                "OAuth grant is revoked, or after a period of inactivity. Generate a "
                "fresh one:\n"
                "  1. Go to https://developer.tastytrade.com → My Apps → your app\n"
                "  2. Re-run the OAuth authorization flow to mint a new refresh token\n"
                "  3. Run `msts-trader login --broker tastytrade` again with the new token\n\n"
                "Tip: pass --creds-file path/to/creds.json so you don't have to paste it."
            )
        if "invalid_client" in low or "unauthorized" in low:
            return (
                "Tastytrade rejected your provider secret (invalid_client).\n"
                "Double-check the provider/client secret from developer.tastytrade.com → "
                "My Apps. Make sure no surrounding quotes were included."
            )
        if "not found" in low and "account" in low:
            return (
                "That Tastytrade account number wasn't found on this session.\n"
                "Leave the account id blank to auto-pick your first account, or copy "
                "the exact number from your Tastytrade dashboard."
            )

    if broker == "alpaca":
        if "forbidden" in low or "unauthorized" in low or "401" in low or "403" in low:
            return (
                "Alpaca rejected your API key/secret.\n"
                "  - Confirm you generated the key for the right environment "
                "(paper vs live) and answered the paper prompt to match.\n"
                "  - Regenerate the pair at https://alpaca.markets if unsure.\n"
                "  - Make sure no surrounding quotes were included in the values."
            )

    if broker == "ibkr":
        if "connect" in low or "refused" in low or "timeout" in low:
            return (
                "Couldn't reach TWS / IB Gateway.\n"
                "  - Is TWS or IB Gateway running and logged in?\n"
                "  - API enabled? Configure → API → Enable ActiveX and Socket Clients\n"
                "  - Host/port correct? (Gateway paper 4002, TWS paper 7497, etc.)\n"
                "  - For a Dockerised Gateway, confirm the port is published to the host."
            )

    if broker == "schwab":
        if "token" in low or "invalid_grant" in low or "expired" in low:
            return (
                "Schwab authorization failed or the token expired.\n"
                "Schwab refresh tokens last 7 days. Delete "
                "~/.msts-trader/schwab_token.json and re-run "
                "`msts-trader login --broker schwab` to re-authorize in the browser."
            )

    # Fallback: surface the raw error but framed.
    return f"login failed: {msg}"
