# Changelog

All notable changes to **msts-trader** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the project is pre-1.0, minor versions (0.x.0) may introduce
behaviour changes; patch versions (0.x.y) are fixes and docs.

## [Unreleased]

## [0.15.0] — 2026-06-16

### Fixed
- **IBKR protective stops were completely non-functional** (regression in
  0.13.0/0.14.0). `place_stop`, `open_stops`, and `cancel_order` had been
  indented one level too deep and were silently parsed as dead nested
  functions inside a module-level helper instead of methods on the `IBKR`
  class. Any stop path raised `'IBKR' object has no attribute 'open_stops'`
  (seen as `stop reconcile skipped: open_stops failed (...)` at the end of a
  rebalance). The three methods are now bound to the class, and a structural
  test asserts they stay methods so this can't regress again. The bug was
  invisible to the suite because the stop tests exercise the paper broker
  (IBKR needs a live TWS socket); the misindent still compiled.

### Added
- **Telegram credentials can now live in `config.toml`** via `telegram_token`
  and `telegram_chat_id`, instead of only the `MSTS_TELEGRAM_TOKEN` /
  `MSTS_TELEGRAM_CHAT_ID` environment variables. The env vars still work and
  are used as a fallback when the config keys are absent.
- **`--dry-run` now sends a notification too** when a notify target is
  configured. The message is clearly headed `DRY-RUN preview · N orders
  (nothing sent)` so a webhook/Telegram recipient can't mistake a preview for
  a live fill. Lets you wire up and confirm a webhook without placing orders.

### Changed
- **Failed notifications are now surfaced instead of swallowed.** A configured
  webhook or Telegram channel that doesn't deliver is reported as
  `notify failed: <channel>` rather than silently doing nothing — so a dead
  URL, a bad token, or an n8n test webhook that wasn't actively listening is
  visible. `notifications.notify()` now returns `(sent, failed)`.

## [0.14.0] — 2026-06-13

### Added
- **Stop adapters for Tradier, IBKR, and Schwab** — `supports_stops` is now
  implemented on 6 of 7 brokers (paper, tastytrade, alpaca, tradier, ibkr,
  schwab). Each places a GTC SELL STOP, lists open stops, and cancels by id:
  Tradier via `type=stop` REST, IBKR via `ib_insync.StopOrder`, Schwab via
  the schwab-py STOP order spec. All whole-share (fractional stop quantity
  rounds DOWN — the residual fraction stays unprotected rather than
  over-selling). Hyperliquid stays unsupported by design (perps use
  trigger-order semantics; the equity weights CSV never routes there).
  These are SDK-pattern implementations — validate each with a 1-share stop
  before relying on it live.
- **`examples/pnl-unified.toml`** documents copy-trading the full Unified
  composite from a `ticker,weight,stop_pct` CSV (hydra slots carry the
  1.5%-below-entry stop policy; commingled core/apex lines do not).

## [0.13.0] — 2026-06-12

### Added
- **Live-broker stop adapters**: `supports_stops` is now implemented for
  **Tastytrade** (`OrderType.STOP` + `stop_trigger`, GTC) and **Alpaca**
  (`StopOrderRequest`, GTC) — both whole-share only (fractional stop
  quantity rounds DOWN; the residual fraction stays unprotected rather
  than over-selling). `open_stops` filters live stop orders; `cancel_order`
  by id. Not yet validated against live accounts — paper-validate a
  1-share stop before relying on it.

### Fixed
- **IBKR on Python 3.14**: `import ib_insync` crashed with
  "RuntimeError: There is no current event loop" — its dependency
  `eventkit` calls `get_event_loop()` at import time, and Python 3.14
  removed implicit loop creation. The event-loop shim now runs *before*
  the import, not just before `IB()`. This hit `uv tool install` users
  in particular, since uv tools default to the newest Python; the
  `--python 3.13` workaround is no longer needed.
- **Protective stops on partial reduce / add-on buys**: a SELL trimming
  a position cancelled its stop without re-placing one for the remaining
  shares, and a BUY adding to an existing position placed a stop for the
  added quantity only. Reconcile now anchors on `broker.positions()`
  post-trade: the remainder gets a re-anchored stop at current price,
  add-ons protect the full position.
- **release.yml**: PyPI upload uses `skip-existing` — re-pushed tags
  (e.g. lightweight→annotated conversion) re-fire the workflow and PyPI
  400s on same-version re-uploads; already-published now counts as success.

## [0.12.0] — 2026-06-12

### Added
- **Protective stop orders**: the CSV accepts an optional `stop_pct`
  column (fraction below entry, e.g. `0.015` = 1.5%). After a BUY
  fills, a GTC SELL STOP is placed at `fill x (1 - stop_pct)` and
  reconciled on every run — stops are cancelled when the position is
  exited or reduced (a resting stop with no position would open a
  short) and replaced when a new fill re-anchors the entry. Brokers
  expose this behind a new `supports_stops` flag with `place_stop` /
  `open_stops` / `cancel_order`; the paper broker simulates the full
  lifecycle, others warn-and-skip instead of failing the rebalance.
  Stops fire broker-side: no local intraday watcher is needed (note:
  stop-market guarantees execution, not price — overnight gaps fill
  through the stop).
- **`rebalance --threshold-mode nav|position`** (or `threshold_mode`
  in the config): choose the drift denominator. `nav` (default,
  unchanged) measures a ticker's drift against the whole book;
  `position` measures it against the line itself — required for
  scaled/composite books whose small lines (e.g. 1.8% of NAV) could
  never move 4% of NAV and would be frozen forever under nav-mode.
- **`examples/pnl-unified.toml`**: two-lane execution config for a
  multi-engine composite (weights-expressible engines via CSV here;
  stop-dependent engines via their own runner).

## [0.11.0] — 2026-06-12

### Added
- **`rebalance --min-weight X`** (or `min_weight` in the config): CSV
  rows with `0 < weight < X` are ignored entirely — no buy, and an
  existing position in that ticker is left untouched (not exit-swept).
  An explicit weight of `0` keeps its sell-it-all meaning.
- **`rebalance --allocation X`** (or `allocation` in the config): size
  the target weights against a fixed dollar amount instead of the full
  account NAV — run a $50k strategy sleeve inside a $200k account.
  Drift is measured against the allocation; capped at NAV (use
  leveraged weights, not an oversized allocation, for gross >100%).
  `multi` accepts a top-level `allocation` and a per-`[[account]]`
  override.

### Fixed
- **IBKR login crashed with "There is no current event loop in thread
  'MainThread'"** on new Python interpreters (3.12 deprecated implicit
  event-loop creation; 3.14 removed it — exactly what `uv tool install`
  picks up). The adapter now creates and sets an asyncio loop before
  ib_insync needs one.
- **Schwab OAuth callback mismatch**: the default callback URL was
  `https://127.0.0.1:8182/` (trailing slash) while schwab-py's
  recommended registration — and most real registrations — is
  `https://127.0.0.1:8182`. Schwab matches the redirect URI character
  for character, so the slash mismatch produced an error page on
  schwab.com (or a post-authorization "token expired" failure). Default
  is now slash-less everywhere; the login flow and README now say
  loudly that the value must EXACTLY match the registered callback, and
  the Schwab login-error hints explain the mismatch case.

### Docs
- Documented the optional `SCHWAB_CALLBACK_URL` and `IBKR_ACCOUNT_ID`
  env vars / creds-file keys (both already worked); added them to
  `examples/creds.example.json`.

## [0.10.0] — 2026-06-11

### Added
- **Market-on-close orders** (`rebalance --moc`, or `moc = true` in the
  config file): orders fill in the exchange closing auction instead of
  immediately — for target weights computed against closing prices.
  Supported on **Alpaca** (`TimeInForce.CLS`), **IBKR** (`orderType MOC`),
  **Schwab** (`MARKET_ON_CLOSE`), and **paper** (simulated). MOC is
  whole-share only; quantities round down. Brokers without a closing-
  auction order type (Tastytrade, Tradier, Hyperliquid) are refused
  up front rather than silently downgraded, and the CLI refuses MOC
  submission within ~10 minutes of the close (exchanges cut off around
  15:50 ET). Adapters expose a `supports_moc` capability flag.
- **`login --reauth`** — force a fresh OAuth flow even when a cached
  token exists. For Schwab this deletes the cached token file so the
  browser authorization re-runs, restarting the 7-day refresh-token
  clock: run it on a weekend to guarantee auth works through the whole
  trading week.
- **uv support**: `.python-version` (3.13) pins the dev interpreter,
  `uv.lock` is committed for reproducible dev environments, and the
  README documents `uv tool install msts-trader` (users) and
  `uv sync --all-extras` / `uv run pytest` (development).

### Tests
- 360 passing (+12): MOC order construction per adapter (CLS tif /
  MOC orderType / MARKET_ON_CLOSE spec, whole-share rounding,
  sub-share skip), MOC support matrix, CLI refusal on unsupported
  brokers, paper MOC dry-run, and Schwab `--reauth` token clearing.

## [0.9.7] — 2026-06-11

### Added
- **Tastytrade certification (sandbox) environment support.** Set
  `TT_TEST=1` (env var or creds-file key, also accepts `true`/`yes`/
  `test`/`sandbox`/`cert`) to connect to Tastytrade's cert API instead of
  production. Cert-issued OAuth keys are rejected by production with
  "refresh token has been revoked or has expired" — previously there was
  no way to use them at all. The flag round-trips through `login`, the
  keychain, headless env creds, and `--creds-file`.
- **`client_secret` accepted as a creds-file alias for the provider
  secret** — it's what Tastytrade's developer portal calls it.

### Improved
- **`✓ loaded N value(s)` now lists the loaded key names** (never
  values), so a duplicated or misspelled key in a creds file — which
  silently collapses the count (e.g. 3 entries → "loaded 2") — is visible
  at a glance.
- The tastytrade login error path now hints at `TT_TEST=1` when
  production rejects the grant, and the README documents the
  cert-vs-production split.

## [0.9.6] — 2026-06-10

### Fixed
- **`msts-trader --help` crashed on Windows consoles with a legacy code
  page** (cp1252/cp437): the help text contains `→` and click writes it
  straight to stdout, dying with `UnicodeEncodeError`. stdout/stderr now
  degrade unencodable characters to `?` instead of crashing.
- **Tastytrade: fractional-order fallback misreported the quantity.** When
  a fractional order was rejected (`fractional_trading_invalid_symbol`)
  and resubmitted as whole shares, the result still reported the original
  fractional quantity (e.g. 10.5 instead of the 10 actually sent), so the
  fill log recorded the wrong amount.
- **Tradier / Schwab / IBKR: a legitimate `0` balance fell through `or`
  fallback chains.** Worst case: a maxed-out margin account with
  `stock_buying_power: 0` (Tradier) reported cash as phantom buying power,
  mis-sizing buys. Balance parsing now uses first-non-None semantics
  (`first_present` in `brokers/base.py`).
- **Alpaca: a missing order id came back as the string `"None"`** instead
  of `None`.
- **Paper: a lowercase order ticker booked a position that then valued at
  $0** — `place_market` stored raw-case keys while `quote()`/`set_quote()`
  uppercase, so the price lookup missed. Tickers are now normalised.

### CI / Tests
- CI now also runs on `windows-latest` (Python 3.13). The prompt layer,
  console encoding, and path handling all behave differently there, and
  three tests + the `--help` crash only reproduced on Windows.
- Fixed 3 Windows-only test failures (TOML backslash paths; `ask_secret`
  tests not pinning the flaky-terminal branch). 342 total (+8): zero-balance
  regression tests for Tradier/Schwab/IBKR, Tastytrade fractional-fallback
  quantity, Alpaca missing order id, paper lowercase ticker, cp1252 help,
  and the flaky-terminal prompt path.

## [0.9.5] — 2026-06-07

### Fixed
- **Stale exported secret shadowed `--creds-file` and the login prompt.** A
  revoked `TT_REFRESH_TOKEN` (or any creds var) left in the shell silently
  won over both the fresh token in a `--creds-file` and the interactive
  prompt, so users kept hitting "refresh token revoked" no matter what they
  pasted. Two fixes: (1) an explicit `--creds-file` now loads with
  `overwrite=True`, so the file wins over a stale env var; (2) `ask_secret`
  prints a `[notice] using <VAR> from the environment` line when it sources a
  value from the env, so a stale exported secret can't masquerade as fresh
  input. Reported from the field (Tastytrade login).

### Tests
- 334 total (+2): `--creds-file` overrides a stale env var (paper broker);
  `ask_secret` announces env-sourced values.

## [0.9.4] — 2026-06-06

### Fixed
- **CSV with spaces around the header names** (e.g. `" ticker , weight "`,
  common from spreadsheet exports / manual edits) was wrongly rejected as
  "no targets parsed". The header *check* normalised names but row lookups
  used the raw spaced keys. Row keys are now stripped + lowercased, so
  padded/odd-cased headers resolve. Found by an adversarial-input sweep.

### Tests
- 332 total (+6): spaced headers, CRLF line endings, extra columns,
  scientific-notation weights, percent-sign rejection, and a zero-buy
  (all-sells) book through margin-aware (no divide-by-zero).

## [0.9.3] — 2026-06-06

### Changed
- Metadata: add the `tradier` keyword (it was shipped but unlisted) and
  bump `Development Status` from Alpha to **Beta** (326 tests, three
  brokers live-tested).

### CI
- Added `pip check` (dependency-tree consistency), `python -m build`, and
  `twine check dist/*` (package + long-description metadata validity) to
  the CI matrix, and extended `ruff` to lint `tests/` too. Catches a
  broken package/metadata before it can reach a release.

## [0.9.2] — 2026-06-06

### Added
- **`py.typed` marker (PEP 561)** — the package ships type hints
  throughout; it now advertises them so downstream type-checkers (mypy,
  pyright) actually use them.
- Top-level `--help` now has a description of what the tool does (the CLI
  group was previously bare).
- README: documented the exit codes (0 success / 1 error / 2 market
  closed) for scripting.

## [0.9.1] — 2026-06-06

### Fixed
- **Fully-invested (100%) books were trimmed ~3% on cash accounts.** With
  margin-aware now on by default, a non-leveraged book (e.g. 60/40) on a
  cash/paper account where buying power ≈ NAV got scaled to 97%, leaving
  3% idle. Two root causes, both fixed:
  - The 0.97 safety cushion was applied to the *fit check*; it now applies
    only when actually scaling *down* an over-BP book. A book that fits
    within 100% of buying power is left alone (full deployment).
  - Buy share quantities rounded half-up, which could push a book a few
    cents over BP and spuriously trigger scaling. Buys now round **down**,
    so a book never over-commits from rounding.
  Found by a fresh-install end-to-end sweep.

### Tests
- 284 total (+3): 100% book on a cash account deploys fully (no trim),
  over-BP book is cushioned below the limit, re-confirm tests reworked
  with realistic (≤100% notional) margin rates.

## [0.9.0] — 2026-06-06

### Changed
- **Margin-aware sizing is now ON by default** (matching a production
  live runner, where `MARGIN_AWARE_SIZING` is always on). It's the safer
  default — it prevents the broker rejecting the tail of a leveraged
  order set and distorting the allocation. Pass `--no-margin-aware`
  (or `margin_aware = false` in config) to disable. Applies to both
  `rebalance` and `multi`.
- **Free when the book fits:** a notional pre-check short-circuits before
  any broker margin query — buying power consumed by a long buy is at
  most its notional, so if the notional already fits available BP, no
  scaling is possible and the per-order dry-runs are skipped. So
  default-on adds no API calls / latency in the common steady-state case;
  the real-margin queries run only when the book is actually tight.

### Tests
- 282 total (+1): default-on does not query broker margin when the book
  already fits. Verified live (dry-run) on the 1.60x Tastytrade book —
  default-on, no queries, clean preview; `--no-margin-aware` restores the
  warn-only behavior.

## [0.8.4] — 2026-06-06

### Added
- **Margin-aware re-confirm pass** (completes parity with a production
  live runner). With real broker margin, after the first uniform scale the
  broker is re-queried on the now-smaller book and the buys are scaled
  again if non-linear margin tiers still push it over (bounded to a few
  passes). One cumulative message is reported regardless of pass count.
  The notional path stays single-pass (linear → exact). `apply_margin_aware`
  now returns the scale factor and takes `add_warning` so the caller can
  emit a single summary.

### Tests
- 281 total (+3): multi-pass re-confirm converges and fits real BP, exactly
  one cumulative scale message (not one per pass), notional broker stays
  single-pass.

## [0.8.3] — 2026-06-06

### Fixed
- **Error messages containing brackets were mangled by Rich markup.**
  e.g. "no [[account]] entries in the config" rendered as "no [] entries",
  and any broker error payload with `[...]` could be eaten. All
  dynamic text in coloured output is now Rich-escaped (`_fail`, preview
  warnings/blockers, execution failure reasons, login errors, the
  multi-account table, broker-init / creds-file errors).

### Tests
- 278 total (+9, found the bug above): dust-delta skip, margin-aware
  "fits via sell proceeds" note, creds-file empty-key / comments-only /
  missing-file errors, Hyperliquid env creds, multi no-accounts /
  no-CSV-source guards (the no-accounts test pins the bracket fix).

## [0.8.2] — 2026-06-06

### Added
- **Real per-order margin for IBKR and Tradier** (joining Tastytrade).
  `--margin-aware` now sizes off the broker's actual margin numbers on:
  - IBKR — `whatIfOrder().initMarginChange` summed across buys.
  - Tradier — order preview (`preview=true`) `margin_change` (cost fallback).
  Alpaca and Schwab keep the buying-power approximation, which already
  encodes the Reg-T 2× multiplier and is exact for non-leveraged ETFs.
  Every path falls back to the notional estimate if real margin is
  unavailable. Real margin only matters for leveraged ETFs (TBT, EDZ).

### Tests
- 269 total (+6): Tradier margin_requirement (margin_change, cost
  fallback, error→None), IBKR margin_requirement via a faked socket
  (sum, failure→None, missing-field→None).

## [0.8.1] — 2026-06-06

### Changed
- **Margin-aware sizing now uses the broker's *real* margin on
  Tastytrade.** `--margin-aware` queries Tastytrade's order dry-run
  (`buying_power_effect.change_in_buying_power`) per buy and sizes off the
  actual buying-power requirement — capturing leveraged-ETF margin rates
  (TBT, EDZ, …) that a notional estimate misses. This matches what a
  production live runner does. Falls back to the notional approximation
  automatically when the broker can't compute it (e.g. market closed) or
  doesn't expose margin — no crash, never sizes on partial data.
- Scaling logic moved out of `build_preview` into `diff.apply_margin_aware`
  (broker-aware) so the real-vs-estimated path lives in one tested place.
- Clearer messaging: when margin-aware runs, the generic "re-run with
  --margin-aware" hint is removed and replaced with what it actually did
  — either "(real broker margin / estimated): scaled buys by X%" or
  "buys fit $Y (incl. $Z sell proceeds) — no scaling needed".

### Tests
- 263 total. apply_margin_aware: notional scale-to-fit (weight-preserving),
  real-margin path scales harder than notional, no-op when it fits.
  Verified live (dry-run) against the 1.60x Tastytrade book — clean
  fallback + coherent messaging.

## [0.8.0] — 2026-06-06

### Added
- **Margin-aware uniform sizing (`--margin-aware`).** For leveraged /
  margin books: if gross buys exceed available buying power (broker BP +
  sell proceeds), scale every buy by one uniform factor so the whole book
  fits — preserving relative weights — instead of letting the broker
  reject the tail of the order set and distort the allocation. No-op when
  the sells already fund the buys. Closes the main gap vs a production
  live-trading runner for leveraged books. Also settable in config
  (`margin_aware = true`) and honored by `multi`.

### Changed
- **Orders now execute sells before buys, always.** Proceeds settle/free
  buying power before the buys submit — required for correctness on cash
  accounts (unsettled-funds rejections), and lowers peak margin usage on
  margin accounts. Within each side, larger dollar moves go first.

### Tests
- 261 total (+3): sells-ordered-before-buys, margin-aware scales to fit
  BP (weight-preserving), margin-aware off just warns. Verified live
  (dry-run) against a real 1.60x Tastytrade book.

## [0.7.1] — 2026-06-05

Solidity pass — no user-facing change.

### Changed
- Login dispatch is now a `_LOGIN_FLOWS` dict instead of an if/elif
  chain, pinned by a test to exactly cover `SUPPORTED`. A new broker can
  no longer ship wired into the factory but missing its login flow (the
  class of bug that broke Hyperliquid in 0.3.x).

### Tests
- 258 total (+16): cross-broker consistency — every broker in SUPPORTED
  is present in the factory, the env-creds builder (returns None, no
  crash), and the login dispatch; unknown names still error cleanly.

## [0.7.0] — 2026-06-05

### Added
- **Tradier adapter** (beta) — stdlib-only (urllib, no new dependency).
  Works against production or the free Tradier **sandbox**
  (`TRADIER_SANDBOX=1`), so it's easy to verify end-to-end without real
  money. Auto-discovers the account number; whole-share equity market
  orders. Login wizard + headless env (`TRADIER_ACCESS_TOKEN` /
  `TRADIER_ACCOUNT_ID` / `TRADIER_SANDBOX`).

### Tests
- 242 total (+12 Tradier): parsing verified against mocked HTTP —
  balances, BP fallbacks, positions (list / single-object / "null"),
  quotes (list / single / zero-skip), order preview & rejection.

## [Unreleased — folded into 0.7.0]

### Tests
- Coverage pass for previously-untested pure helpers and edge branches
  (no behaviour change): `ibkr._reject_reason` (KID 201 / 10349
  surfacing), `ibkr._f` / `_midpoint`, `hyperliquid._coin`
  normalisation, diff short-position-left-untouched + qty-rounds-to-0,
  `safety.parse_asof` bad-date, `runstate` corrupted-file recovery,
  notifications Discord/Slack/generic payload routing. 221 tests.

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

[Unreleased]: https://github.com/markudevelop/msts-trader/compare/v0.13.0...HEAD
[0.13.0]: https://github.com/markudevelop/msts-trader/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/markudevelop/msts-trader/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/markudevelop/msts-trader/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/markudevelop/msts-trader/compare/v0.9.7...v0.10.0
[0.9.7]: https://github.com/markudevelop/msts-trader/compare/v0.9.6...v0.9.7
[0.9.6]: https://github.com/markudevelop/msts-trader/compare/v0.9.5...v0.9.6
[0.9.5]: https://github.com/markudevelop/msts-trader/compare/v0.9.4...v0.9.5
[0.9.4]: https://github.com/markudevelop/msts-trader/compare/v0.9.3...v0.9.4
[0.9.3]: https://github.com/markudevelop/msts-trader/compare/v0.9.2...v0.9.3
[0.9.2]: https://github.com/markudevelop/msts-trader/compare/v0.9.1...v0.9.2
[0.9.1]: https://github.com/markudevelop/msts-trader/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/markudevelop/msts-trader/compare/v0.8.4...v0.9.0
[0.8.4]: https://github.com/markudevelop/msts-trader/compare/v0.8.3...v0.8.4
[0.8.3]: https://github.com/markudevelop/msts-trader/compare/v0.8.2...v0.8.3
[0.8.2]: https://github.com/markudevelop/msts-trader/compare/v0.8.1...v0.8.2
[0.8.1]: https://github.com/markudevelop/msts-trader/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/markudevelop/msts-trader/compare/v0.7.1...v0.8.0
[0.7.1]: https://github.com/markudevelop/msts-trader/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/markudevelop/msts-trader/compare/v0.6.0...v0.7.0
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
