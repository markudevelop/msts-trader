# msts-trader

Paste a target-weights CSV, preview the rebalance, execute it on your own
brokerage account. Multi-broker, local-only, no key custody.

7 brokers (Tastytrade, Alpaca, Tradier, IBKR, Schwab, Hyperliquid, paper),
leverage + margin-aware sizing (real broker margin, on by default),
sells-before-buys, optional protective stops (`stop_pct` column, 6/7
brokers), multi-account, headless (cron / GitHub Actions),
notifications, idempotency, and a `--json` API. Licensed PolyForm
Noncommercial.

```
$ msts-trader
Paste CSV (ticker,weight), then Ctrl+D:
ticker,weight
SPY,0.42
GLD,0.18
SHV,0.20
EEM,0.20
^D
✓ loaded 4 targets.

tastytrade · account 5W******  ·  NAV $48,213.42  ·  cash $2,150.00  ·  BP $46,290.00
Market: open  ·  closes in 23 min

           Rebalance preview
┃ Symbol ┃ Current % ┃ Target % ┃   Δ $ ┃ Action                  ┃ Note ┃
┃ SPY    ┃    18.2%  ┃   42.0%  ┃ +$11k ┃ BUY  22.00 @ ~$521.34   ┃      ┃
┃ EEM    ┃    31.5%  ┃   20.0%  ┃  -$5k ┃ SELL 119.00 @ ~$47.21   ┃      ┃
...

Execute 4 orders on tastytrade? [y/N]: y
[1/4] SPY  BUY  22.00 @ MKT ...  ROUTED  id=4f8...

Done. tastytrade: sent 4, failed 0
```

## Supported brokers

| Broker      | Status                   | Auth                      | Install |
|-------------|--------------------------|---------------------------|---------|
| Paper       | shipped, tested          | local file                | built-in |
| Tastytrade  | shipped, **live-tested** | OAuth refresh token       | built-in |
| Alpaca      | shipped, **live-tested** | API key + secret          | built-in |
| Tradier     | shipped, beta            | bearer token (REST)       | built-in (free sandbox to test) |
| IBKR        | shipped, **live-tested** | TWS / IB Gateway socket   | `pip install "msts-trader[ibkr]"` |
| Schwab      | shipped, beta            | OAuth2 + browser callback | `pip install "msts-trader[schwab]"` |
| Hyperliquid | shipped, **experimental**| API-wallet private key    | `pip install "msts-trader[hyperliquid]"` |

- **Live-tested** = connect / balances / positions / quotes / order path
  verified against a real account (Tastytrade & Alpaca filled real
  1-share orders; IBKR verified read + dry-run).
- **Beta** (Schwab, Tradier) = parsing logic is unit-tested (Tradier
  against mocked HTTP) but no live fill confirmed by the author. Tradier
  has a free sandbox (`TRADIER_SANDBOX=1`) — easy to verify yourself.
- **Experimental** (Hyperliquid) = crypto perps DEX; the adapter is built
  on the public SDK but has not been run against a live account. Test on
  testnet (`HL_TESTNET=1`) with tiny size first.

**IBKR + EU accounts:** an EU-regulated IBKR account cannot trade
US-domiciled ETFs (KID/PRIIPs, Error 201). US stocks may still be
cancelled by an account Order Preset (Error 10349 → fix in TWS Global
Configuration → Presets). Tastytrade and Alpaca have neither limit.

Open a GitHub issue to prioritise a broker (Tradier and a ccxt-based
crypto adapter are likely next).

## Install

```bash
pip install msts-trader
```

or with [uv](https://docs.astral.sh/uv/) (installs the CLI into an
isolated environment, no venv juggling):

```bash
uv tool install msts-trader
```

Python ≥3.11 required (uv fetches a suitable Python automatically).

### Optional brokers

IBKR and Schwab require extra dependencies. Install them only if you
plan to use that broker:

```bash
pip install "msts-trader[ibkr]"         # adds ib_insync + nest_asyncio
pip install "msts-trader[schwab]"       # adds schwab-py
pip install "msts-trader[hyperliquid]"  # adds hyperliquid-python-sdk + eth-account
pip install "msts-trader[all]"          # everything
```

(with uv: `uv tool install "msts-trader[all]"`)

> **Note (IBKR + uv tool, versions ≤ 0.12.0):** `uv tool install` picks
> the newest Python it can find (currently 3.14), where IBKR auth in
> older releases failed with a "no current event loop" error from
> `ib_insync`/`eventkit`. Fixed in releases after 0.12.0; if you're stuck
> on an older version, pin Python 3.13:
>
> ```bash
> uv tool install --python 3.13 --reinstall "msts-trader[all]"
> ```
>
> `uv run` from a source checkout was never affected — it honors the
> [.python-version](.python-version) pin.

Install from source:

```bash
git clone https://github.com/markudevelop/msts-trader.git
cd msts-trader
pip install -e ".[all]"
```

or with uv — `uv sync` creates the venv, pins Python to
[.python-version](.python-version), and installs everything:

```bash
git clone https://github.com/markudevelop/msts-trader.git
cd msts-trader
uv sync --all-extras
uv run msts-trader --help
```

## One-time setup

You provide your own broker credentials. They are stored in your OS
keychain (macOS Keychain / Windows Credential Manager / libsecret on
Linux) and never leave your machine.

### Tastytrade

1. Sign in at https://developer.tastytrade.com → **My Apps**
2. Create an OAuth application — copy the **provider secret**
3. Run their OAuth authorization flow to obtain a **refresh token**
4. Look up your **account number** in the Tastytrade web dashboard (optional)
5. Run:

```bash
msts-trader login --broker tastytrade
```

Using Tastytrade's **certification (sandbox) environment**? Cert-issued
keys are rejected by production (and vice versa) — set `TT_TEST=1` (env
or creds file) so msts-trader connects to the cert API instead.

### Alpaca

1. Sign in at https://alpaca.markets (paper or live)
2. Account → API keys → generate a new pair
3. Run:

```bash
msts-trader login --broker alpaca
```

You choose paper vs live at login time.

### Tradier

```bash
msts-trader login --broker tradier
```

Get an access token at https://developer.tradier.com — a **free sandbox**
token works for end-to-end testing. Your account number is
auto-discovered if you leave it blank. Choose sandbox or production at
login. Headless: `TRADIER_ACCESS_TOKEN` / `TRADIER_ACCOUNT_ID` /
`TRADIER_SANDBOX`.

### IBKR

```bash
pip install "msts-trader[ibkr]"
msts-trader login --broker ibkr
```

On versions ≤ 0.12.0 installed via `uv tool`, use `--python 3.13` — see
the [install note](#optional-brokers) about IBKR on Python 3.14.

You'll be asked for host, port, and client id of a running TWS or IB
Gateway. Defaults:

- TWS live: `127.0.0.1:7496`
- TWS paper: `127.0.0.1:7497`
- Gateway live: `127.0.0.1:4001`
- Gateway paper: `127.0.0.1:4002`
- Dockerised Gateway: usually `127.0.0.1:4002` (whatever you mapped)

Before logging in, enable Configure → API → **Enable ActiveX and Socket
Clients** in your TWS / Gateway. msts-trader connects, lists your
managed accounts, and confirms NAV.

### Schwab

```bash
pip install "msts-trader[schwab]"
msts-trader login --broker schwab
```

Requires a Schwab Developer app (https://developer.schwab.com) with the
callback URL set to `https://127.0.0.1:8182`. msts-trader pops a
browser window, you authorize, and the token JSON is stored in your OS
keychain. Legacy plaintext token files at
`~/.msts-trader/schwab_token.json` are migrated into the keychain and
removed on next Schwab use. Schwab refresh tokens expire every 7 days
— re-run `msts-trader login --broker schwab` when that happens.

> **The callback URL must match your app's registration EXACTLY** —
> character for character, trailing slash included. Schwab treats
> `https://127.0.0.1:8182` and `https://127.0.0.1:8182/` as different
> URLs: a mismatch shows an error page on schwab.com during
> authorization, or fails the flow afterwards with "authorization
> failed or the token expired". If your app is registered with a
> different callback (port, slash, …), enter that exact value at the
> login prompt or set `SCHWAB_CALLBACK_URL`.

Don't wait for it to expire mid-week: run

```bash
msts-trader login --broker schwab --reauth
```

on a Saturday or Sunday to force a fresh browser authorization and
restart the 7-day clock, guaranteeing auth works through the whole
trading week.

After the first successful Schwab login, the app key, app secret, callback
URL, account hash, and OAuth token are all stored in your OS keychain. You can
delete the original `--creds-file`; future `login --broker schwab --reauth`
runs reuse those stored app credentials and only refresh the Schwab OAuth
token.

**Multiple linked Schwab accounts under one OAuth login:** first-time
`login` lists linked books and lets you pick a default (stored as
`account_hash` in the keychain). Change it later with
`msts-trader login --broker schwab --account 6789`. List books any time with
`msts-trader status --broker schwab --all-accounts`. Target a non-default
book per command with `--account` (full number, unique last-4, or hash) —
same selector works on `rebalance` / `liquidate` / `doctor`, config
`account_id = "…"`, env `SCHWAB_ACCOUNT_ID` / `SCHWAB_ACCOUNT_HASH`, or
`account = "…"` on a `multi` `[[account]]` row.

### Paper (offline simulator)

```bash
msts-trader login --broker paper
```

No real money, no broker connection required. The book persists in
`~/.msts-trader/paper_state.json` between sessions. Reset any time with
`msts-trader paper-reset`.

**Real quotes for paper (optional):** install the yfinance extra and the
paper broker fetches live prices for any ticker it has no stored quote for —
so a paper book (or a paper sleeve experiment) runs against real market
prices with zero setup:

```bash
pip install "msts-trader[yfinance]"
```

Explicitly seeded/booked prices always win, fetched prices are cached in the
paper state, and `MSTS_PAPER_YF=0` forces the old offline behavior. Without
the extra installed nothing changes — unquoted tickers are simply skipped.

The first `login` you complete becomes the default broker. Override per
command with `--broker NAME`, or change the default by logging in again.

## Daily usage

1. Get your CSV. Click **Copy CSV** on the supported weights site, or
   build your own:

   ```csv
   ticker,weight,stop_pct
   SPY,0.42,
   GLD,0.18,0.05
   EEM,0.20,
   SHV,0.20,
   ```

   - `weight` is a fraction of NAV (e.g. `0.42` = 42%), not a percent.
   - Sum **≤ 1.0** holds the remainder as cash; sum **> 1.0** is leverage
     (e.g. `1.60` = 160% gross, financed on margin — see
     [Leveraged weights](#leveraged-weights)).
   - No shorts: negative weights are rejected.
   - `stop_pct` is **optional** — a protective-stop column. See
     [Protective stops](#protective-stops).
   - Comments starting with `#` are ignored (and `# asof: <iso>` enables
     the stale-CSV guard).

2. Run:

```bash
msts-trader                       # uses default broker
msts-trader --broker alpaca       # explicit broker
```

3. Paste the CSV, hit `Ctrl+D` (`Ctrl+Z` then Enter on Windows).
4. Review the preview carefully.
5. Type `y` to execute, anything else to cancel.

### Useful flags

```bash
msts-trader rebalance --dry-run                       # preview only, never sends
msts-trader rebalance --yes                           # skip the confirm prompt
msts-trader rebalance --threshold 0.02                # tighter rebalance (default 4%)
msts-trader rebalance --csv-file targets.csv          # read from a file
msts-trader rebalance --moc                           # market-on-close orders (see below)
msts-trader rebalance --order-type limit-chase        # work each order as a limit pegged to the mid (see below)
msts-trader rebalance --min-weight 0.01               # ignore CSV rows under 1% weight
msts-trader rebalance --allocation 50000              # weights apply to $50k, not full NAV
msts-trader --broker paper rebalance --csv-file ...   # test against paper
```

- **`--moc` (market-on-close):** orders fill in the exchange closing
  auction instead of immediately — useful when your target weights are
  computed against closing prices. Supported on **Alpaca, IBKR, Schwab,
  and paper** (Tastytrade/Tradier/Hyperliquid have no MOC order type —
  the CLI refuses rather than silently downgrading). MOC orders are
  whole-share only, and exchanges stop accepting them around **15:50 ET**,
  so submit before then. Also available as `moc = true` in the config file.
- **`--order-type limit-chase`:** instead of one market order per leg, each
  order is worked as a **LIMIT pegged to the live mid** — re-quote and reprice
  every few seconds (`--chase-interval`, default 5s; polled every
  `--chase-poll`, default 1s) up to `--chase-retries` times (default 5), then
  **fall back to a market order** for whatever hasn't filled (disable with
  `--no-chase-fallback`). `--chase-aggression 0.001` nudges the limit 0.1% past
  the mid toward the fill side to improve the hit rate (default `0` = pure mid).
  The goal is execution quality — pay near the mid instead of crossing the whole
  spread. Safety: the prior limit is **cancelled before each reprice** (and the
  chase aborts rather than risk two live orders if a cancel fails), partial
  fills only re-submit the remainder, and no resting order is ever left behind.
  **RTH only** (the market fallback assumes the regular session), supported on
  **all brokers**; any that can't chase warn once and use market orders. Also
  available as `order_type = "limit-chase"` in the config file (and in a `multi`
  config, including per-`[[account]]` override).
- **`--whole-shares`:** round every order *down* to whole shares (buys never
  exceed target, sells never exceed the held quantity). Use it for an IBKR
  account — or any broker/account — without fractional-trading permission on
  the API, which otherwise rejects fractional orders with **error 10243**
  ("Fractional-sized order cannot be placed via API"). Applied before the
  preview, so what you see is exactly what's sent (and margin-aware scaling
  re-rounds to whole shares too). Also available as `whole_shares = true` in
  the config file.
- **`--stop-pct`:** a *default* protective stop (fraction below entry, e.g. `--stop-pct 0.015`
  = 1.5%) applied to every bought/held target that has no per-row `stop_pct`. An explicit
  `stop_pct` column value always wins; exits (weight 0) get none. Use it when your weights feed
  carries only ticker+weight but you still want every position stopped. Also `stop_pct` in the
  config file (and per-`[[account]]` in a `multi` config). **Stops are opt-in:** with no per-row
  `stop_pct` and no `--stop-pct`, **no stops are placed** — and a rebalance never strips an
  existing stop off a still-held position (only orphan stops with no position are cancelled).
- **Post-trade verification (on by default):** after fills + stop reconciliation, the account is
  re-fetched and the rebalance diff is run again; any leg that would *still* trade is one that
  didn't converge (partial fill, failed close, rejected, not-yet-settled). Reported on the
  console and as a follow-up notification (`✅ converged` / `🔴 NOT converged — N legs, X% of
  NAV`), and added to `--json` as a `verify` object. Broker-agnostic. `--no-verify` to skip.
- **Self-heal (on by default):** when verification finds the book off target, the residual legs
  are **re-executed once and re-verified**, so a single `rebalance` converges the account instead
  of just reporting the miss. Bounded by `--heal-passes` (default 1), **market-open only**, and
  each pass runs through the normal executor (re-bought legs get their protective stops). A leg
  that can't fill stops after the cap and is reported 🔴. `--no-self-heal` for report-only.
- **`--min-weight`:** rows with `0 < weight < min-weight` are ignored
  entirely — no buy, and an existing position in that ticker is *not*
  exit-swept either. An explicit weight of `0` still means "sell it all".
  Useful when the CSV carries many tiny weights you don't want to trade.
- **`--allocation`:** size the weights against a fixed dollar amount
  instead of the whole account — e.g. run a $50k strategy sleeve inside
  a $200k account. Positions in tickers *not* in the CSV are still
  exited (the sweep is account-wide), so keep sleeve and non-sleeve
  tickers disjoint, use `--no-sweep`, or rebalance with a CSV that lists
  everything you hold. Capped at NAV; use leveraged weights (sum > 1.0)
  for gross exposure above the allocation.
- **`--no-sweep`:** touch **only** the tickers in the CSV and leave every
  other held position untouched — the safe way to run a sleeve inside a
  **mixed account**. The default (`--sweep`) treats the CSV as the complete
  book and liquidates anything held but unlisted. Under `--no-sweep`, a
  held-but-unlisted position shows in the preview as `kept — not in targets`
  with no order; to actually **close** a rotated-out name, list it with
  weight `0`. (When sourcing from a published feed, have the publisher emit
  `weight=0` rows for exited tickers so closes stay explicit.)

### Safety, automation & output flags

```bash
msts-trader rebalance --no-margin-aware       # disable buying-power-fit scaling (on by default)
msts-trader rebalance --max-notional 60000    # refuse if gross buys exceed $60k
msts-trader rebalance --max-stale-hours 36   # refuse if the CSV's `# asof:` is too old
msts-trader rebalance --json                 # machine-readable output (one JSON object)
msts-trader rebalance --quiet                # minimal output for cron logs
msts-trader rebalance --notify-url <webhook> # Discord/Slack/generic ping on execute
msts-trader rebalance --force                # run even if same targets already done today
msts-trader rebalance --config my.toml       # load defaults from a config file
msts-trader rebalance --no-verify            # skip the post-trade convergence check (on by default)
msts-trader rebalance --no-self-heal         # verify only, don't re-execute residual legs (self-heal on by default)
msts-trader rebalance --heal-passes 2        # max self-heal re-execution passes (default 1)
```

- **Idempotency:** identical targets won't trade twice in the same UTC day
  unless you pass `--force` (guards against a cron + manual overlap).
- **Stale guard:** add a `# asof: 2026-06-05T15:45:00Z` comment line to your
  CSV and `--max-stale-hours` refuses to trade on old weights.
- **Notifications:** set `--notify-url` or `MSTS_NOTIFY_URL`
  (Discord/Slack/generic webhook), or `MSTS_TELEGRAM_TOKEN` +
  `MSTS_TELEGRAM_CHAT_ID` (Telegram creds can also go in `config.toml` as
  `telegram_token` / `telegram_chat_id`). A failed webhook never blocks
  trading, but the failure is now reported (`notify failed: webhook`) instead
  of swallowed. `--dry-run` also fires a clearly-labelled preview
  notification, so you can wire up and test a webhook without sending orders.
- **Retries:** transient broker errors (429s, timeouts) are retried with
  backoff; real errors fail fast.

### Config file

Set defaults once in `~/.msts-trader/config.toml` (or pass `--config`):

```toml
broker = "tastytrade"
threshold = 0.04
csv_url = "https://example.com/weights.csv"
max_notional = 60000
max_stale_hours = 36
notify_url = "https://discord.com/api/webhooks/..."
telegram_token = "123456:ABC-DEF..."   # optional, instead of MSTS_TELEGRAM_TOKEN
telegram_chat_id = "987654321"          # optional, instead of MSTS_TELEGRAM_CHAT_ID
margin_aware = true   # default; set false to disable buying-power-fit scaling
moc = false           # set true to always use market-on-close orders
order_type = "market" # or "limit-chase": peg a limit to the mid, reprice, then market-fallback (RTH only)
chase_retries = 5     # limit-chase: reprice attempts before the market fallback
chase_interval = 5    # limit-chase: seconds to wait for a fill before repricing
chase_poll = 1        # limit-chase: status-poll cadence within each rung (seconds)
chase_aggression = 0  # limit-chase: fraction past the mid toward the fill side (0 = pure mid)
chase_fallback = true # limit-chase: market order for any unfilled remainder
whole_shares = false  # set true to round every order to whole shares (IBKR/no-fractional accounts)
min_weight = 0.01     # ignore CSV rows with weight under 1%
stop_pct = 0.015      # default protective stop for rows with no per-row stop_pct (per-row wins)
allocation = 50000    # weights apply to $50k instead of full NAV
quiet = false
```

Resolution order for any setting: CLI flag > environment > config file > default.

### Other commands

```bash
msts-trader status                  # NAV, positions, market status (default broker)
msts-trader status --json           # machine-readable account snapshot (monitoring)
msts-trader status --creds-file x   # headless status, no keychain
msts-trader doctor                  # health-check creds/connectivity/market for each broker
msts-trader doctor --broker ibkr    # check one broker
msts-trader brokers                 # list supported + configured brokers
msts-trader logout --broker alpaca  # clear stored creds for one broker
msts-trader paper-reset             # reset paper book to starting cash
msts-trader --version
```

`doctor` is the fastest way to diagnose a broker: it shows, per broker,
whether credentials are present, whether it connects, your NAV, position
count, and a sample SPY quote — so permission/connectivity problems
(like the IBKR KID block) surface immediately.

## What it does

- Parses your CSV into `{ticker: target_weight}`.
- Pulls live NAV, cash, buying power, and current positions from your broker.
- Quotes every relevant symbol via the broker's market-data API.
- Computes the dollar delta per ticker against the drift threshold (default
  4% of NAV). **Execution scope (`--rebalance-scope`, default `whole-book`):**
  the threshold is a *trigger* — if any line breaches it, the whole book is
  snapped to target (more turnover, higher CAGR on momentum books). Pass
  `--rebalance-scope per-ticker` to trade only the breaching lines and leave
  the rest (lower turnover, better Sharpe/drawdown).
- Sells tickers no longer in your targets.
- Sizes buys at the current quote, rounded to 2 decimals where the
  broker supports fractional MARKET orders.
- Shows the full plan and waits for `y` before sending anything.
- Submits MARKET DAY orders. Logs results to `~/.msts-trader/fills/`.

## Headless / automated (cron, GitHub Actions)

Everything works two ways:

- **Manual:** `msts-trader` → paste CSV → confirm with `y`.
- **Headless:** drive it entirely from files / env vars + flags — no
  paste, no confirm prompt, no interactive `login`, no keychain.

The headless one-liner:

```bash
msts-trader rebalance \
  --broker tastytrade \
  --creds-file creds.json \
  --csv-url https://example.com/your-weights.csv \
  --yes
```

- `--creds-file` — JSON or `KEY=VALUE` file with your credentials (or
  just export the env vars; both work). See
  [`examples/creds.example.json`](examples/creds.example.json).
- `--csv-file PATH` or `--csv-url URL` — the target weights, instead of
  pasting.
- `--yes` — skip the confirmation prompt (required for unattended runs).
- `--dry-run` — preview only, never sends (great for a first test).

Credentials resolve in this order: `--creds-file` / environment first,
then the OS keychain. So a server or CI box that has never run `login`
works as long as the env vars are set.

Ready-to-use templates are in [`examples/`](examples/):

- [`rebalance-cron.sh`](examples/rebalance-cron.sh) — a cron wrapper.
- [`github-action-rebalance.yml`](examples/github-action-rebalance.yml)
  — a scheduled GitHub Actions workflow.

**Broker notes for automation:**

- **Tastytrade**, **Alpaca**, and **Tradier** are pure REST/OAuth → work
  in GitHub Actions or any server.
- **IBKR** needs a running TWS / IB Gateway on a machine you control →
  use cron on that machine, not GitHub Actions.

The market-hours guard still applies: a headless run outside US regular
hours exits without trading, so a daily schedule is safe.

### Exit codes

For scripting, `rebalance` / `multi` use:

| Code | Meaning |
|------|---------|
| `0`  | Success — executed, or nothing to do (within drift / dry-run / duplicate) |
| `1`  | Error — bad/missing creds, malformed CSV, a blocker (e.g. `--max-notional`), stale CSV, or a partial/failed execution |
| `2`  | Market closed or not in a regular-hours session (equities) |

## Multiple accounts

There are two multi-account models:

### Same login, several linked books

One OAuth / API session that can see more than one brokerage account
(Schwab linked accounts, Tastytrade, Tradier, IBKR managed accounts).
Every broker implements `list_linked_accounts` / `use_account`; single-account
brokers (Alpaca key, paper, Hyperliquid wallet) return one entry.

```bash
# At login: pick a default when several books are linked (interactive),
# or set it explicitly (also works headless)
msts-trader login --broker schwab --account 6789
msts-trader login --broker tastytrade --account 5W12345

# See every linked account under the current login
msts-trader status --broker schwab --all-accounts

# Target one book for a single command (full number/id or unique last-4)
msts-trader status --broker schwab --account 6789
msts-trader rebalance --broker schwab --account 6789 --dry-run
msts-trader liquidate --broker tastytrade --account 5W… --dry-run
```

Config / env alternatives for a single run:

| Surface | Example |
|---------|---------|
| CLI | `--account 6789` |
| rebalance config | `account_id = "6789"` |
| Schwab env | `SCHWAB_ACCOUNT_ID=6789` or `SCHWAB_ACCOUNT_HASH=…` |
| Tasty / Tradier / IBKR env | `TT_ACCOUNT_ID` / `TRADIER_ACCOUNT_ID` / `IBKR_ACCOUNT_ID` |

Ambiguous last-4 matches fail with the list of masked accounts — nothing
executes against the wrong book.

### Several logins / brokers (`multi`)

Run the same target weights across several accounts in one pass with the
`multi` command and a TOML config that lists each account's broker and
creds file. Rows may also share one creds file and differ only by
`account` (same-login multi-account):

```toml
# multi-account.toml
csv_url = "https://example.com/weights.csv"
threshold = 0.04
max_notional = 60000

[[account]]
name = "tasty-main"
broker = "tastytrade"
creds_file = "~/.msts-trader/tasty.json"

[[account]]
name = "alpaca-live"
broker = "alpaca"
creds_file = "~/.msts-trader/alpaca.json"

[[account]]
name = "schwab-taxable"
broker = "schwab"
creds_file = "~/.msts-trader/schwab.json"
account = "1234"   # last-4 or full number

[[account]]
name = "schwab-ira"
broker = "schwab"
creds_file = "~/.msts-trader/schwab.json"   # same OAuth login
account = "5678"
```

```bash
msts-trader multi --config multi-account.toml --dry-run    # preview all
msts-trader multi --config multi-account.toml --yes        # execute all
msts-trader multi --config multi-account.toml --json --yes # machine-readable
```

Accounts run sequentially; each gets its own credentials (no cross-leak),
the same idempotency + safety checks as a single run, and a combined
summary at the end. `multi` never prompts — `--yes` is required to
execute, `--dry-run` to preview. See
[`examples/multi-account.toml`](examples/multi-account.toml).

## Multiple strategies in one account

msts-trader has no per-strategy position ledger — it reads the *account's*
position in a ticker and sizes against that. Run two strategies as two separate
rebalances and they will fight over any ticker they share: each sees the other's
shares as its own drift and trades them away.

Four ways to run several strategies against one login. Native sleeves are
the most capable; the others need no state at all.

### Native sleeves: `--sleeve` (order tally per strategy)

The direct answer to "multiple strategies in one account, alongside my manual
trades": every order sent under `--sleeve NAME` is recorded in a local ledger
(`~/.msts-trader/sleeves/`), and the sleeve's **confirmed fills** accumulate
into a per-ticker share tally. A sleeve run sizes against ONLY its own tally —
other sleeves' shares and anything you traded by hand are invisible to it, so
two strategies can hold the SAME ticker and nobody sells anyone else's shares:

```bash
msts-trader sleeve invest momo 50000     # bootstrap: give the sleeve its capital
msts-trader sleeve invest carry 30000

msts-trader rebalance --sleeve momo  --csv-file momo.csv  --yes
msts-trader rebalance --sleeve carry --csv-file carry.csv --yes

msts-trader sleeve list                  # every sleeve, tallies, cash, policy
msts-trader sleeve invest momo 25000     # scale the winner up (its only new money)
msts-trader sleeve divest momo 10000     # take capital back out of its cash
msts-trader sleeve base momo 20%         # or: size off 20% of ACCOUNT NAV
msts-trader sleeve cap momo $50000       # ceiling — never deploy more than this
msts-trader sleeve adopt momo SPY 100    # assign already-held shares
msts-trader sleeve show momo             # tallies + live NAV + P&L vs contributed
msts-trader sleeve reconcile             # tallies + cash vs account, per ticker
```

**Sizing bases** (`sleeve base NAME ...`): the default is `own-nav` — the
compounding book described above. `20%` sizes off account NAV instead (the
sleeve's capital floats with the whole account — an explicit opt-in, no
`invest` needed), and `$50000` is a static figure (the old `--allocation`
semantics, persistent and explicit: gains above it are trimmed, drawdowns
topped up from the account — use it only when you want exactly that).
`sleeve cap NAME $X|X%|off` bounds any base from above — "give the strategy
at most this much", with gains beyond the cap parking in the sleeve's cash.
A configured sleeve refuses a per-run `--allocation`. Manage by shares via
`sleeve adopt`/`release`. If the sleeves' combined virtual cash ever exceeds
the account's real cash, the run warns (in a margin account the excess is
just the cross-margin borrow; a cash account would reject those buys).

**A sleeve manages its own money.** `sleeve invest` sets its virtual cash (no
real money moves — everything stays in the one cross-margined account), and
from then on the sleeve sizes against its **own NAV** (cash + holdings):
gains compound inside the sleeve, and a drawdown is its own to dig out of —
the account's other money is never pulled in unless you `invest` more.
Confirmed fills move the sleeve's cash exactly (sells add, buys subtract, at
the actual fill price). A static `--allocation` would instead trim every gain
back to the fixed figure and buy losses back up with account money, so it is
refused on an invested sleeve (and merely warned about on a legacy one).

How it stays safe (details in
[docs/design-strategy-sleeves.md](docs/design-strategy-sleeves.md)):

- **The invariant is `Σ sleeve tallies ≤ account position`** — the gap is
  *unassigned* (your manual book, by construction). The tool structurally
  cannot touch shares it didn't buy: the sweep only exits *tally-owned*
  positions, and sells clamp to the tally.
- **Tallies move only on confirmed fills** (`order_status`'s `filled_qty`),
  never on ordered quantity. Partial fills and resting/MOC orders settle
  idempotently on later runs.
- **Fail-closed reconciliation:** if tallies ever claim more than the account
  holds (you manually sold sleeve shares, a corporate action), the run refuses
  and `sleeve reconcile` / `sleeve adjust` is the explicit fix — the tool never
  guesses whose shares vanished.
- An account-level `rebalance` (no `--sleeve`) on a ledgered account is
  refused — its sweep would sell every sleeve's holdings.

v1 limits: market orders only (no `--order-type limit-chase`), no protective
stops under `--sleeve` (stops are sized account-wide today), and `multi` has no
sleeve support yet — run one `rebalance --sleeve` per strategy. Bootstrap each
sleeve with `sleeve invest` (else weights size against full account NAV), and
add `--threshold-mode position` if the sleeve is small relative to the account.

### Merge the sleeves into one book (works on every broker)

Sum the strategies yourself and send **one** combined CSV. No ledger is needed:
a single rebalance lands the account on the exact combined target, and a ticker
held by two strategies nets out correctly by construction.

[`examples/merge_sleeves.py`](examples/merge_sleeves.py) does the arithmetic.
Each sleeve keeps its own weights CSV (weights are fractions of that sleeve) plus
a dollar allocation; every line becomes a fraction of total NAV:

```bash
# momo.csv:  SPY 0.60, GLD 0.40      carry.csv:  SPY 0.50, SHV 0.50
python examples/merge_sleeves.py 50000 momo.csv 30000 carry.csv > combined.csv
#  -> SPY 0.5625, GLD 0.25, SHV 0.1875      (SPY = (30k + 15k) / 80k)

msts-trader rebalance --csv-file combined.csv --dry-run
```

- Keep the default `--sweep` only while the merged CSV **is** the complete
  book — sharing the account with anything else needs the next section.
- Drift is measured on the *combined* book, so a small sleeve may never breach
  4% of total NAV. Add `--threshold-mode position` (or a lower `--threshold`)
  if you want small sleeves to trade.
- A ticker carries only one `stop_pct`; where sleeves disagree on a shared name
  the tightest stop wins.
- One run rebalances everything at once. If one sleeve is daily and another
  monthly, run the merge daily anyway — the monthly sleeve simply won't move
  until its weights change.

### Alongside your own manual trades

Running the algo book inside the account you also trade by hand — keeping one
cross-margined book instead of splitting off a second PM account — is the same
merge with a fence around it:

```bash
python examples/merge_sleeves.py 50000 momo.csv 30000 carry.csv > combined.csv
msts-trader rebalance --csv-file combined.csv     --allocation 80000 --no-sweep --threshold-mode position --dry-run
```

- `--allocation 80000` pins the algo book to its own dollars, so manual P&L
  moving account NAV never resizes it. **Scaling in is this one number.**
- `--no-sweep` stops msts-trader liquidating everything it didn't put there.
- `--threshold-mode position` gates drift per line — an $80k book's lines
  rarely move 4% of a $200k NAV.
- Margin-aware sizing still reads account-wide buying power, so algo buys
  compete with your manual positions for BP. Usually what you want in a
  cross-margined account; `--no-margin-aware` opts out.

The one rule: **the algo tickers must stay disjoint from the ones you trade by
hand.** msts-trader reads the *account's* position in a ticker, so a sleeve
holding SPY would resize the SPY you bought yourself — and a `stop_pct` on that
line would put a stop across your manual shares too, since stops are sized to
the full holding. Where they collide, use a second symbol for the same
exposure: SPY vs VOO/IVV, GLD vs IAU, QQQ vs QQQM.

Retiring a sleeve under `--no-sweep` takes one extra step, because an unlisted
ticker is *left alone*: zero its weights and run once to close the positions
before dropping it from the merge command. `merge_sleeves.py` emits an explicit
`0` row for anything that nets to zero, so keep weight-0 exits in the sleeve
CSVs rather than deleting the row.

### Separate brokerage accounts (no bookkeeping at all)

Tastytrade, IBKR and Schwab all allow several accounts under one login. Give each
strategy its own account and the broker keeps the ledger for you — nothing to
merge, nothing to reconcile:

```bash
msts-trader rebalance --broker schwab --account 1234 --csv-file momo.csv  --dry-run
msts-trader rebalance --broker schwab --account 5678 --csv-file carry.csv --dry-run
```

(`multi` is not the tool here: it runs *one* CSV across many accounts. For a
different book per account, use one `rebalance --account …` per strategy.)

### Disjoint tickers per sleeve (`--allocation` + `--no-sweep`)

If you'd rather not merge — each strategy on its own schedule, its own cron
line — separate runs against the *same* account also work, as long as no two
sleeves touch the same symbol:

```bash
msts-trader rebalance --csv-file momo.csv  --allocation 50000 --no-sweep --threshold-mode position
msts-trader rebalance --csv-file carry.csv --allocation 30000 --no-sweep --threshold-mode position
```

`--allocation` sizes each sleeve against its own dollars, and `--no-sweep` stops
it liquidating the other sleeve's positions (list a rotated-out name with weight
`0` to close it). Equivalent exposure is usually available under a second symbol
— SPY vs VOO/IVV, GLD vs IAU, QQQ vs QQQM — so overlapping strategies can often
be made disjoint for a few bps of tracking difference. Attribution then comes
free: every ticker in `~/.msts-trader/fills/*.jsonl` belongs to exactly one
sleeve. NAV, buying power and stop sizing remain account-wide, so this only
holds while the universes stay disjoint.

## Protective stops

Add an optional **`stop_pct`** column to the CSV and msts-trader places a
GTC SELL STOP under each position it buys:

```csv
ticker,weight,stop_pct
SPY,0.42,
GLD,0.18,0.05
WGMI,0.02,0.015
```

- `stop_pct` is a **fraction below the fill price**, not a price:
  `0.05` = 5%, `0.015` = 1.5%. Must be in `(0, 0.5)`; a blank cell means
  no stop.
- After a BUY fills, a GTC SELL STOP is placed for the filled quantity at
  `fill_price × (1 − stop_pct)`.
- Stops are **reconciled every rebalance**: on a SELL the existing stop is
  cancelled (and re-placed on the remaining quantity if you still hold
  some and the target still wants a stop), so a resting stop never outlives
  its position and turns into a naked short.
- Supported on **6 of 7 brokers** — Tastytrade, Alpaca, Tradier, IBKR,
  Schwab, and paper. **Hyperliquid** has no stop support: the column is
  ignored with a one-time warning, weights still execute. Verify a broker
  honors stops with a 1-share test before relying on it.

See [`examples/pnl-unified.toml`](examples/pnl-unified.toml) for a full
copy-trade + stop setup.

## Leveraged weights

Target weights are fractions of your account NAV. They **can sum to more
than 1.0** — that's leverage. For example a book that sums to 1.60
(160% gross exposure, 1.60x) sizes each position at `weight × NAV`, and
the amount over 100% is financed on margin:

```csv
ticker,weight
QQQ,0.3123
GLD,0.2537
TBT,0.1480
...        # sums to ~1.60 = 160% gross
```

The preview shows `Gross target exposure: 160% (1.60x)`. **Margin-aware
sizing is on by default** (matching a production live runner): if the
buys exceed your available buying power (broker BP plus the proceeds from
the sells, which execute first), msts-trader scales **every buy by one
uniform factor** so the whole book fits — preserving your relative
weights — instead of letting the broker reject the tail of the order set
piecemeal and distort your allocation. When the sells already fund the
buys, nothing is scaled (and it's free — a notional pre-check skips the
broker margin queries unless the book is actually tight). Pass
`--no-margin-aware` to disable.

Where the broker exposes it, this uses the broker's **real** per-order
margin so leveraged-ETF rates (TBT, EDZ, …) are sized exactly — the same
approach a production live runner uses:

| Broker | Margin source |
|--------|---------------|
| Tastytrade | real — order dry-run `buying_power_effect` |
| IBKR | real — `whatIfOrder` initial-margin change |
| Tradier | real — order preview `margin_change` |
| Alpaca / Schwab | buying power (already encodes the Reg-T 2× multiplier) |

Real per-order margin only *matters* for leveraged ETFs; for plain ETFs,
notional-vs-buying-power is already exact. All paths are weight-preserving,
and any failure to get real margin falls back to the notional estimate
automatically (never sizes on partial data).

With real margin it also **re-confirms**: after scaling, it re-queries the
broker on the now-smaller book and scales again if non-linear margin tiers
still push it over (up to a few passes), then reports one cumulative
scale. The notional path is linear, so it's exact in a single pass.

Orders always execute **sells before buys**, so proceeds free up buying
power before the buys submit (required on cash accounts, lower peak
margin on margin accounts).

Two things to know for a **fresh account**:

- Positions smaller than the drift threshold (default **4% of NAV**)
  won't be established on the first run — they look "within drift" of a
  zero holding. For initial setup of a book with small sleeves, lower it:
  `msts-trader rebalance --threshold 0.01`.
- A single weight above 3.0 (300%) is rejected as a likely
  percentage-paste mistake (e.g. `31.23` instead of `0.3123`).

## What it does NOT do (yet)

- Pre-market or after-hours execution for equities. Refuses outside
  09:30–16:00 ET (crypto via Hyperliquid trades 24/7).
- Shorting. Negative weights are rejected.
- Options or futures.
- Protective stops or limit-chase under `--sleeve` (sleeve runs are
  market-order only in v1) — see [Multiple strategies in one
  account](#multiple-strategies-in-one-account).
- Active stop *management* (Hydra/Fusion-style trailing watchers). Static
  protective stops **are** supported via the `stop_pct` CSV column — see
  [Protective stops](#protective-stops).
- Scheduling itself (use cron / GitHub Actions — see
  [Headless](#headless--automated-cron-github-actions)).

## Troubleshooting

### Can't paste or type during `msts-trader login`?

Some terminals — VS Code, Cursor, and **Windows Terminal / Windows
consoles** — don't reliably forward input to hidden-password prompts
(Python's `getpass`). The cursor sits there and nothing registers.

msts-trader detects these terminals and switches to **visible input**
automatically (you'll see a `[notice]`), so you can paste your secret —
it's just shown on screen as you type. But the cleanest fix is to not
type secrets at all:

#### Best: use a credentials file (`--creds-file`)

Create a small file — JSON or `KEY=VALUE` — with your credentials:

`tt_creds.json`
```json
{
  "TT_PROVIDER_SECRET": "your-provider-secret",
  "TT_REFRESH_TOKEN": "your-refresh-token",
  "TT_ACCOUNT_ID": "your-account-number"
}
```

or `tt_creds.env`
```
TT_PROVIDER_SECRET=your-provider-secret
TT_REFRESH_TOKEN=your-refresh-token
TT_ACCOUNT_ID=your-account-number
```

then:

```bash
msts-trader login --broker tastytrade --creds-file tt_creds.json
```

No prompts, no terminal quirks, works identically on every OS. Delete
the file afterwards — the credentials are now in your OS keychain.

Lowercase keys (`provider_secret`, `api_key`, etc.) also work, and
`client_secret` is accepted as an alias for the provider secret (it's
what Tastytrade's portal calls it). Add `TT_TEST=1` if the keys are from
Tastytrade's certification (sandbox) environment. For
Alpaca use `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY` / `APCA_PAPER`;
for IBKR `IBKR_HOST` / `IBKR_PORT` / `IBKR_CLIENT_ID` /
`IBKR_ACCOUNT_ID` (optional — auto-discovered when omitted); for Schwab
`SCHWAB_APP_KEY` / `SCHWAB_APP_SECRET` / `SCHWAB_CALLBACK_URL`
(optional — defaults to `https://127.0.0.1:8182`; must exactly match
your app's registered callback, trailing slash included).

#### Or: set environment variables

Mind the shell — this trips people up:

- **macOS / Linux (bash/zsh):**
  ```bash
  export TT_PROVIDER_SECRET="..."
  export TT_REFRESH_TOKEN="..."
  export TT_ACCOUNT_ID="..."
  ```
- **Windows PowerShell** (the Windows Terminal default — `export` and
  `set` do NOT work here):
  ```powershell
  $env:TT_PROVIDER_SECRET="..."
  $env:TT_REFRESH_TOKEN="..."
  $env:TT_ACCOUNT_ID="..."
  ```
- **Windows cmd.exe** (do NOT wrap values in quotes — cmd keeps them):
  ```cmd
  set TT_PROVIDER_SECRET=...
  set TT_REFRESH_TOKEN=...
  set TT_ACCOUNT_ID=...
  ```

Then run `msts-trader login --broker tastytrade` in the **same** window.
(msts-trader strips accidental surrounding quotes, but PowerShell vs cmd
syntax still matters.)

### `login failed: invalid_grant / Grant revoked`

This is Tastytrade telling you the **refresh token is no longer valid** —
it was regenerated, the OAuth grant was revoked, or it expired from
inactivity. It is not a bug in msts-trader; the token simply needs to be
re-minted:

1. https://developer.tastytrade.com → My Apps → your app
2. Run the OAuth authorization flow again to get a **new refresh token**
3. `msts-trader login --broker tastytrade` (or `--creds-file`) with the new token

You'll also see this error if you use **certification (sandbox) keys**
against production — cert keys only work with `TT_TEST=1` set.

## Security

- Your broker credentials live only in your OS keychain on your own
  machine. The app does not phone home, does not log credentials, and
  is not connected to any service operated by the author.
- The author of this app cannot view, recover, or revoke your broker
  access. Revoke via your own broker's API-app dashboard if a key leaks.
- Trades are user-initiated: every execution requires you to paste a
  CSV and confirm with `y`. There is no background trading loop.

Full details and how to report a vulnerability: [SECURITY.md](SECURITY.md).

## Disclaimer

This tool sends real orders to your live brokerage account. You are
responsible for the CSV you paste and the rebalance you confirm. Past
performance of any signal source is not indicative of future results.
The author makes no warranty of any kind; use at your own risk.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for the full version history. Each
released tag also has a [GitHub Release](https://github.com/markudevelop/msts-trader/releases)
with the same notes and the built wheel attached.

## Development

```bash
git clone https://github.com/markudevelop/msts-trader.git
cd msts-trader
pip install -e ".[all,dev]"
pytest -v          # 350+ tests, a couple of seconds
ruff check msts_trader
```

or with uv (uses the Python pinned in `.python-version`):

```bash
uv sync --all-extras
uv run pytest -v
uv run ruff check msts_trader
uv run ruff format --check msts_trader   # or `ruff format msts_trader` to apply

```

The test suite covers:

- CSV parser (header validation, weights, leverage, comments, dup/neg guards)
- Diff math (drift threshold, exits, warnings, blockers, BP overrun, leverage)
- Market hours (RTH/pre/after/closed, holidays through 2028, weekends)
- Paper broker end-to-end (cash accounting, position lifecycle, dry-run, persistence)
- Broker protocol conformance (every adapter exposes the required attrs + methods)
- Keychain + env-derived credentials (per-broker, quote stripping, fallbacks)
- Safety (max-notional cap, stale-CSV guard), retry/backoff, idempotency
- Config file parsing, notifications formatting/dispatch
- CLI (help, version, brokers list, doctor, login, no-creds clean exit)

Live brokerage adapters are not exercised against real APIs in CI — they
need credentials and can move real money. The tests verify structure;
you verify fills.

## License

[PolyForm Noncommercial License 1.0.0](LICENSE).

You may use, modify, and share this software for any **noncommercial
purpose** — personal trading, research, education, hobby projects.
**Selling, hosting as a paid service, or otherwise commercializing
this software or derivative works is not permitted** without a separate
commercial license. Contact the author if you need one.
