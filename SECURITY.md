# Security

msts-trader places real orders on real brokerage accounts. This document
describes how it handles your credentials and money, and how to report a
vulnerability.

## Reporting a vulnerability

Please **do not** open a public issue for a security problem. Instead use
GitHub's private vulnerability reporting:

- https://github.com/markudevelop/msts-trader/security/advisories/new

Include the version (`msts-trader --version`), your OS, and steps to
reproduce. You'll get an acknowledgement, and a fix or mitigation will be
released as a patch version with credit (if you want it).

## Security model

**Credentials never leave your machine.**

- Broker credentials (Tastytrade refresh token, Alpaca keys, Tradier
  token, IBKR connection details, Schwab OAuth, Hyperliquid key) are
  stored only in your operating system's keychain (macOS Keychain,
  Windows Credential Manager, or libsecret on Linux) via the `keyring`
  library — or read at runtime from environment variables / a
  `--creds-file` you control.
- The tool does **not** phone home, send telemetry, or transmit your
  credentials, positions, or orders to any service operated by the
  author. The only network calls are directly to **your** broker's API
  (and an optional notification webhook **you** configure).
- Because the author never holds your credentials, the author cannot
  view, recover, or revoke your broker access. Revoke a leaked key from
  your broker's own API/app dashboard.

**Trades are user-initiated.**

- Every execution requires you to supply a CSV and either confirm with
  `y` or pass `--yes`. There is no hidden background trading loop.
- Orders are submitted exactly once — they are **never** retried, so a
  transient error can't double a fill.
- Safety rails: a market-hours guard (equities trade only in RTH), a
  configurable drift threshold, an optional `--max-notional` cap, a
  stale-CSV guard (`--max-stale-hours`), same-day idempotency, and
  margin-aware sizing that scales a leveraged book to fit buying power.

## Handling credential files

- `--creds-file` reads a JSON or `KEY=VALUE` file. Keep it private
  (`chmod 600`) and delete it after `login` stores the values in your
  keychain.
- In CI (e.g. GitHub Actions), put credentials in encrypted repository
  **secrets** and pass them as environment variables — never commit them.
- Logs at `~/.msts-trader/fills/` record order results (ticker, side,
  quantity, status, order id) — not credentials.

## Supported versions

This is pre-1.0 software; only the latest released version receives
security fixes. Keep current with `pip install -U msts-trader`.

## Disclaimer

This tool sends real orders to your live brokerage account. You are
responsible for the CSV you provide and the rebalance you confirm. It is
not investment advice, and it comes with no warranty (see the
[LICENSE](LICENSE)). Use at your own risk.
