# Changelog

All notable changes to **msts-trader** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the project is pre-1.0, minor versions (0.x.0) may introduce
behaviour changes; patch versions (0.x.y) are fixes and docs.

## [Unreleased]

_Nothing yet._

## [0.6.0] — 2026-06-05

### Added
- **Multi-account: `multi` command.** Run the same target weights across
  several accounts in one pass, driven by a TOML config with `[[account]]`
  tables (each naming a broker + creds file). Accounts run sequentially
  with isolated credentials (no cross-leak via env), the same
  idempotency + safety checks as a single run, and a combined table/JSON
  summary. `--yes` required to execute, `--dry-run` to preview.
  See `examples/multi-account.toml`.
- `creds_file.broker_kwargs(broker, get)` + `broker_kwargs_from_file`:
  build broker kwargs from an isolated per-account mapping (expands `~`),
  so several accounts can be constructed in one process safely.

### Fixed
- The buying-power overrun warning hardcoded "Tastytrade"; it's now
  broker-agnostic ("the broker's pre-flight may scale orders down").

### Tests
- 200 total (+4): multi dry-run across two accounts, `--yes` requirement,
  isolated file-based creds + env fallback.

## [0.5.3] — 2026-06-05

### Fixed
- **Schwab would fail on the first real call.** The client was built with
  schwab-py's default `enforce_enums=True`, which rejects the plain
  string `"positions"` passed to `get_account(fields=["positions"])` —
  so `balances()` and `positions()` raised. Now constructs the client
  with `enforce_enums=False` so raw-string fields work. (Found by
  reviewing the adapter against the live SDK signatures; order path
  `place_order` / `equity_buy_market` / `.build()` verified correct.)

## [0.5.2] — 2026-06-05

### Added
- **`status --json`** — machine-readable account snapshot (broker,
  account, NAV/cash/BP, market status, positions with %NAV). Pairs with
  `rebalance --json` for headless monitoring.

### Tests
- 196 total.

## [0.5.1] — 2026-06-05

Production-readiness audit fixes.

### Fixed
- **Market orders are no longer retried** (important). v0.5.0 wrapped
  `place_market` in the retry helper — but a market order is not
  idempotent, so a transient error *after* the broker accepted it would
  double the fill. Orders now submit exactly once; reads
  (balances/positions/quote) keep their retry. The drift gate
  self-corrects a missed fill on the next run.
- **Idempotency records only on clean success.** A run where some/all
  orders failed no longer marks the targets "done for today", so a
  re-run can complete the remaining orders (previously blocked as a
  duplicate unless `--force`).
- **`login --broker hyperliquid`** now works (was "unknown broker" —
  Hyperliquid was env/creds-file only). Added the login wizard.
- **`--csv-url` size cap** (5 MB) so a misconfigured URL can't stream
  something huge into the parser.

### Tests
- 195 total. New: place_market-not-retried safety test.

## [0.5.0] — 2026-06-05

### Added
- **Notifications** — ping a Discord / Slack / generic webhook (or a
  Telegram bot) on execute. `--notify-url` or `MSTS_NOTIFY_URL` /
  `MSTS_TELEGRAM_TOKEN` + `MSTS_TELEGRAM_CHAT_ID`. Failures never block
  trading. Essential for unattended runs.
- **`doctor` command** — per-broker health check: creds present?
  connects? NAV, position count, sample SPY quote. Surfaces
  permission/connectivity problems (e.g. the IBKR KID block) instantly.
- **`--json`** — machine-readable single-object output for rebalance
  (and structured exit), for logging / piping in automation.
- **`--quiet`** — minimal output for cron logs (still prints a one-line
  summary and errors).
- **`--max-notional`** — refuse if gross buys exceed a dollar cap.
- **`--max-stale-hours`** + CSV `# asof: <iso>` — refuse to trade on
  stale weights.
- **Idempotency guard** — identical targets won't execute twice in the
  same UTC day unless `--force` (cron + manual overlap protection).
- **Retry/backoff** — transient broker errors (429s, timeouts, resets)
  are retried; real errors fail fast.
- **Config file** — `~/.msts-trader/config.toml` (or `--config`) for
  defaults: broker, threshold, csv source, limits, notify URL, quiet.
  Resolution: CLI > env > config > default.
- **Hyperliquid adapter** (experimental) — crypto perps DEX via the
  public SDK. `pip install "msts-trader[hyperliquid]"`. Not yet
  live-verified; test on testnet (`HL_TESTNET=1`) with tiny size.

### Tests
- 194 total (+41): safety, retry, runstate, config, notifications, env
  creds, plus Hyperliquid protocol conformance.

### Notes
- Crypto brokers (hyperliquid) skip the RTH market-hours guard (24/7).

## [0.4.0] — 2026-06-05

### Added
- **Fully headless operation** — run unattended from cron / GitHub
  Actions with no paste, no confirm prompt, no interactive `login`, no
  keychain:
  - `rebalance` and `status` accept `--creds-file` (JSON or KEY=VALUE);
    credentials also resolve from environment variables.
  - `_load_broker` builds the broker from env / creds-file first, then
    falls back to the OS keychain — so a box that never ran `login`
    works as long as the env is set (`broker_kwargs_from_env`).
  - `rebalance --csv-url URL` fetches the target CSV over http(s)
    (stdlib, no new dependency), alongside the existing `--csv-file`.
  - `--yes` continues to skip the confirmation for unattended runs.
- **Automation templates** in `examples/`: `creds.example.json`,
  `rebalance-cron.sh`, and `github-action-rebalance.yml`.

### Docs
- New "Headless / automated" README section. Notes that Tastytrade and
  Alpaca run in GitHub Actions, while IBKR needs a local TWS / IB
  Gateway (use cron on that machine).

### Tests
- +12 (`test_broker_env.py`) covering env-derived creds for every
  broker, paper default, quote stripping, and missing-var fallbacks.

## [0.3.8] — 2026-06-05

### Fixed
- IBKR `_reject_reason` now surfaces Error **10349** ("TIF set to DAY
  based on order preset") when it's the only log entry, instead of
  returning a bare `Cancelled`. A live 1-share TSLA test confirmed a
  plain US stock is cancelled by an account Order Preset (10349 only,
  no KID 201) — distinct from the ETF KID/PRIIPs block. The message now
  points at TWS → Global Configuration → Presets.
- `login_errors` maps 10349 / "order preset" to that fix, clarifying it
  is the Order PRESETS config, not the "Bypass Order Precautions" toggle.

### Notes
- Two separate IBKR blockers are now characterised: (1) US ETFs →
  Error 201 KID/PRIIPs (EU retail), (2) US stocks → Error 10349 account
  Order Preset. Tastytrade and Alpaca filled both SHV and TSLA fine.

## [0.3.7] — 2026-06-05

### Fixed
- **IBKR now surfaces the real rejection reason.** Orders that IBKR
  cancels used to return a bare `Cancelled` status. The adapter now
  reads the trade log and returns the actual cause (skipping the
  cosmetic `10349` TIF-preset note), e.g. `IBKR 201: ... does not have
  a KID ...`.

### Changed
- Diagnosed the earlier IBKR cancellation: it was **not** the order
  preset / `10349` (a red herring) but **Error 201 / KID-PRIIPs** — EU
  retail accounts cannot trade US-domiciled ETFs (SPY, QQQ, SHV, GLD)
  without an EU Key Information Document. `login_errors` and the IBKR
  adapter now explain this. This is a brokerage/regulatory limit, not a
  tool bug; Tastytrade and Alpaca trade these tickers fine.

### Known issues
- IBKR on an EU retail account will reject the US-ETF Core/Apex universe
  (KID/PRIIPs). Use Tastytrade or Alpaca for those strategies, trade
  UCITS equivalents, or ask IBKR about elective-professional status.

## [0.3.6] — 2026-06-05

### Added
- **Leverage support.** Target weights may now sum to more than 1.0 —
  that's gross exposure / leverage (e.g. 1.60 = 160% gross, financed on
  margin), which is how the production books are actually sized. Each
  position is sized at `weight × NAV`. The preview shows a "Gross target
  exposure: 160% (1.60x)" line and a margin warning. Previously any CSV
  summing past 1.05 was hard-blocked as "malformed" — that would have
  **rejected real leveraged books outright**.
- **IBKR dry-run = real what-if.** `place_market(dry_run=True)` on IBKR
  now calls `whatIfOrder` and returns the broker's margin / commission
  preview instead of a local stub. (Alpaca has no what-if API, so its
  dry-run stays a local no-op; Tastytrade's dry-run already hits its
  real validation endpoint.)

### Fixed
- **IBKR `quote()` reliability** (found during a live dry-run): the old
  `reqMktData` + fixed-sleep approach often returned only one of several
  symbols and emitted a noisy `Error 300: Can't find EId`. Rewrote
  `quote()` to use batched `reqTickers` (blocks until each snapshot
  populates, cancels cleanly) with a delayed-data fallback
  (`reqMarketDataType(3)`). Verified live: SPY / SHV / GLD all return.

### Changed
- CSV parser per-ticker cap raised from 1.0 to 3.0 (allows leveraged
  single positions; still rejects percentages-pasted-as-whole-numbers).
- `diff.build_preview` blocks only at >5.0x gross (almost certainly
  percentages), warns on any leverage above 1.01x, and keeps the
  cash-drag warning under 0.5x.
- IBKR promoted to **live-tested** in the support matrix after an
  end-to-end read + dry-run against a real TWS account.

### Known issues
- IBKR market orders can be cancelled by TWS with `Error 10349` ("Order
  TIF was set to DAY based on order preset") when the account has API
  order precautions enabled. Enable **Global Configuration → API →
  Precautions → "Bypass Order Precautions for API Orders"** in TWS / IB
  Gateway, or a future release will add a marketable-limit fallback.

### Tests
- 141 total. New leverage cases include the real 160% production book
  (15 tickers, sum 1.60) building without blockers, and the note that
  sleeves under the 4% drift threshold need a lower `--threshold` on a
  fresh account.

## [0.3.5] — 2026-06-05

### Added
- `--creds-file` flag on `login`: load credentials from a JSON or
  `KEY=VALUE` file so secrets never pass through a terminal prompt.
  Works identically on every OS. Accepts lowercase aliases
  (`provider_secret` → `TT_PROVIDER_SECRET`, `api_key` →
  `APCA_API_KEY_ID`, etc.).

### Fixed
- **Windows hidden-input bug** (reported): `getpass` drops paste/typing
  in Windows Terminal and many Windows consoles. The CLI now detects
  these terminals (`WT_SESSION`, `sys.platform == win*`, VS Code,
  Cursor) and switches straight to visible input with a `[notice]`
  instead of leaving an unresponsive cursor.
- **Env-var quote handling**: `set VAR="x"` on Windows cmd captures the
  quotes; values are now quote-stripped (`strip_quotes` / `env_value`),
  and whitespace-only values are treated as empty.
- **Clear auth errors**: `invalid_grant` / "Grant revoked" now prints
  "your Tastytrade refresh token has been revoked or expired — mint a
  new one", with similar guidance for `invalid_client`, Alpaca 401/403,
  IBKR connection-refused, and Schwab token-expired.

### Docs
- Rewrote the troubleshooting section: leads with `--creds-file`,
  documents correct PowerShell (`$env:`) vs cmd (`set`, no quotes) vs
  bash (`export`) syntax, and adds a dedicated `invalid_grant` entry.

### Tests
- 137 total (+28): `test_creds_file.py`, `test_login_errors.py`,
  `test_prompts_quotes.py`.

## [0.3.4] — 2026-06-05

### Fixed
- **VS Code / Cursor prompt bug** (reported): hidden password prompts
  couldn't receive input in those integrated terminals. Added
  `msts_trader/prompts.py` with `ask_secret` that falls back to visible
  input, honours per-broker env vars, and detects VS Code / Cursor.
- **Tastytrade `quote()` returned empty** against a real account: the
  SDK call passed a list where a single symbol string was expected.
  Switched to the batch `get_market_data_by_type(equities=[...])` API.
  Confirmed live (SPY/SHV/GLD prices return correctly).

### Changed
- Tastytrade and Alpaca promoted to **live-tested** in the support
  matrix after end-to-end verification against real accounts.

### Tests
- 109 total (+14 prompt-fallback tests).

## [0.3.3] — 2026-06-05

### Fixed
- Dropped unused `ib_insync.util` and `json` imports (ruff F401) that
  failed CI on the py3.11 matrix.

## [0.3.2] — 2026-06-05

This is the first release to ship the test suite (the 0.3.1 version bump
was folded into this release; no 0.3.1 was published to PyPI).

### Added
- **pytest suite, 95 tests** across CSV parsing, diff math, market
  hours, models, paper broker end-to-end, broker-protocol conformance,
  keychain, and CLI smoke. CI now runs `pytest -v` on py3.11/3.12/3.13.

### Fixed
- `msts-trader login --broker NAME` failed with "No such option:
  --broker" — `--broker` was only on the parent group. Added it to each
  subcommand. (Caught by the new CLI tests.)
- `CredsMissingError` was raised but not caught, producing a traceback
  instead of the friendly "run login first" message.

### Docs
- Support matrix marks Alpaca / IBKR / Schwab as **beta** ("awaiting
  live-fill confirmation"); added a Development section.

## [0.3.0] — 2026-06-05

### Added
- **IBKR adapter** (`msts_trader.brokers.ibkr`) via `ib_insync` —
  connects to TWS / IB Gateway over the API socket (works with a
  Dockerised Gateway). Install with `pip install "msts-trader[ibkr]"`.
- **Schwab adapter** (`msts_trader.brokers.schwab`) via `schwab-py` —
  OAuth2 browser flow, token cached at `~/.msts-trader/schwab_token.json`.
  Install with `pip install "msts-trader[schwab]"`.
- Optional extras: `[ibkr]`, `[schwab]`, `[all]`.
- `msts-trader brokers` now lists all five.

### Changed
- Removed "lifted from msts-live" notes; the project is built only on
  public broker SDKs and is independently maintainable.

## [0.2.0] — 2026-06-05

### Added
- **Multi-broker architecture**: `Broker` protocol in
  `msts_trader/brokers/` with a `make(name, **creds)` factory.
- **Alpaca adapter** (paper or live, fractional shares).
- **Paper broker** — offline local simulator with $100k starting cash,
  persistent JSON book, `paper-reset` command.
- `--broker NAME` on every subcommand; default broker stored in the
  keychain so the bare `msts-trader` keeps working.

### Changed
- **Relicensed** from Apache-2.0 to **PolyForm Noncommercial 1.0.0**.
  Personal / research / hobby use stays unrestricted; commercial use,
  hosted SaaS, or paid derivatives require a separate license.
- Keychain re-keyed per broker (`creds:<broker>` + `default_broker`).
  v0.1 users need to re-run `msts-trader login`.

## [0.1.1] — 2026-06-05

### Fixed
- ruff lint errors in `tasty.py`.
- Opt into Node.js 24 in GitHub Actions to silence the Node 20
  deprecation warning.

### Docs
- README polish; PyPI install confirmed.

## [0.1.0] — 2026-06-05

### Added
- Initial release. Paste a `ticker,weight` CSV → preview the rebalance
  against your live **Tastytrade** account → confirm → execute.
- Commands: `login`, `status`, `rebalance` (default), `logout`.
- 4% drift threshold, exit-on-removed-ticker, BP overrun warning,
  RTH-only guard, fill log at `~/.msts-trader/fills/`.
- Credentials stored in the OS keychain (BYO Tastytrade OAuth app).
- OIDC trusted publishing to PyPI on tag push.

[Unreleased]: https://github.com/markudevelop/msts-trader/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/markudevelop/msts-trader/compare/v0.5.3...v0.6.0
[0.5.3]: https://github.com/markudevelop/msts-trader/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/markudevelop/msts-trader/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/markudevelop/msts-trader/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/markudevelop/msts-trader/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/markudevelop/msts-trader/compare/v0.3.8...v0.4.0
[0.3.8]: https://github.com/markudevelop/msts-trader/compare/v0.3.7...v0.3.8
[0.3.7]: https://github.com/markudevelop/msts-trader/compare/v0.3.6...v0.3.7
[0.3.6]: https://github.com/markudevelop/msts-trader/compare/v0.3.5...v0.3.6
[0.3.5]: https://github.com/markudevelop/msts-trader/compare/v0.3.4...v0.3.5
[0.3.4]: https://github.com/markudevelop/msts-trader/compare/v0.3.3...v0.3.4
[0.3.3]: https://github.com/markudevelop/msts-trader/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/markudevelop/msts-trader/compare/v0.3.0...v0.3.2
[0.3.0]: https://github.com/markudevelop/msts-trader/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/markudevelop/msts-trader/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/markudevelop/msts-trader/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/markudevelop/msts-trader/releases/tag/v0.1.0
