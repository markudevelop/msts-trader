# msts-trader

Paste a target-weights CSV, preview the rebalance, execute it on your own
brokerage account. Multi-broker, local-only, no key custody.

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
| IBKR        | shipped, **live-tested** | TWS / IB Gateway socket   | `pip install "msts-trader[ibkr]"` |
| Schwab      | shipped, beta            | OAuth2 + browser callback | `pip install "msts-trader[schwab]"` |
| Hyperliquid | shipped, **experimental**| API-wallet private key    | `pip install "msts-trader[hyperliquid]"` |

- **Live-tested** = connect / balances / positions / quotes / order path
  verified against a real account (Tastytrade & Alpaca filled real
  1-share orders; IBKR verified read + dry-run).
- **Beta** (Schwab) = passes structural conformance tests in CI but no
  live fill confirmed by the author.
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

Python ≥3.11 required.

### Optional brokers

IBKR and Schwab require extra dependencies. Install them only if you
plan to use that broker:

```bash
pip install "msts-trader[ibkr]"         # adds ib_insync + nest_asyncio
pip install "msts-trader[schwab]"       # adds schwab-py
pip install "msts-trader[hyperliquid]"  # adds hyperliquid-python-sdk + eth-account
pip install "msts-trader[all]"          # everything
```

Install from source:

```bash
git clone https://github.com/markudevelop/msts-trader.git
cd msts-trader
pip install -e ".[all]"
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

### Alpaca

1. Sign in at https://alpaca.markets (paper or live)
2. Account → API keys → generate a new pair
3. Run:

```bash
msts-trader login --broker alpaca
```

You choose paper vs live at login time.

### IBKR

```bash
pip install "msts-trader[ibkr]"
msts-trader login --broker ibkr
```

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
callback URL set to `https://127.0.0.1:8182/`. msts-trader pops a
browser window, you authorize, and the token JSON is written to
`~/.msts-trader/schwab_token.json`. Schwab refresh tokens expire every
7 days — re-run `msts-trader login --broker schwab` when that happens.

### Paper (offline simulator)

```bash
msts-trader login --broker paper
```

No real money, no broker connection. The book persists in
`~/.msts-trader/paper_state.json` between sessions. Reset any time with
`msts-trader paper-reset`.

The first `login` you complete becomes the default broker. Override per
command with `--broker NAME`, or change the default by logging in again.

## Daily usage

1. Get your CSV. Click **Copy CSV** on the supported weights site, or
   build your own:

   ```csv
   ticker,weight
   SPY,0.42
   GLD,0.18
   EEM,0.20
   SHV,0.20
   ```

   - `weight` is a fraction of NAV (e.g. `0.42` = 42%), not a percent.
   - Sum **≤ 1.0** holds the remainder as cash; sum **> 1.0** is leverage
     (e.g. `1.60` = 160% gross, financed on margin — see
     [Leveraged weights](#leveraged-weights)).
   - No shorts: negative weights are rejected.
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
msts-trader --broker paper rebalance --csv-file ...   # test against paper
```

### Safety, automation & output flags

```bash
msts-trader rebalance --max-notional 60000   # refuse if gross buys exceed $60k
msts-trader rebalance --max-stale-hours 36   # refuse if the CSV's `# asof:` is too old
msts-trader rebalance --json                 # machine-readable output (one JSON object)
msts-trader rebalance --quiet                # minimal output for cron logs
msts-trader rebalance --notify-url <webhook> # Discord/Slack/generic ping on execute
msts-trader rebalance --force                # run even if same targets already done today
msts-trader rebalance --config my.toml       # load defaults from a config file
```

- **Idempotency:** identical targets won't trade twice in the same UTC day
  unless you pass `--force` (guards against a cron + manual overlap).
- **Stale guard:** add a `# asof: 2026-06-05T15:45:00Z` comment line to your
  CSV and `--max-stale-hours` refuses to trade on old weights.
- **Notifications:** set `--notify-url` or `MSTS_NOTIFY_URL`
  (Discord/Slack/generic webhook), or `MSTS_TELEGRAM_TOKEN` +
  `MSTS_TELEGRAM_CHAT_ID`. A failed webhook never blocks trading.
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
- Computes the dollar delta per ticker, skips anything within the drift
  threshold (default 4% of NAV).
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

- **Tastytrade** and **Alpaca** are pure REST/OAuth → work in GitHub
  Actions or any server.
- **IBKR** needs a running TWS / IB Gateway on a machine you control →
  use cron on that machine, not GitHub Actions.

The market-hours guard still applies: a headless run outside US regular
hours exits without trading, so a daily schedule is safe.

## Multiple accounts

Run the same target weights across several accounts in one pass with the
`multi` command and a TOML config that lists each account's broker and
creds file:

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

The preview shows `Gross target exposure: 160% (1.60x)` and warns that it
needs a margin account with sufficient buying power. The broker's own
pre-flight (e.g. Tastytrade) will scale the order set down if your
buying power is short.

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
- Active stop management (Hydra/Fusion-style watchers).
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

Lowercase keys (`provider_secret`, `api_key`, etc.) also work. For
Alpaca use `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY` / `APCA_PAPER`;
for IBKR `IBKR_HOST` / `IBKR_PORT` / `IBKR_CLIENT_ID`; for Schwab
`SCHWAB_APP_KEY` / `SCHWAB_APP_SECRET`.

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

## Security

- Your broker credentials live only in your OS keychain on your own
  machine. The app does not phone home, does not log credentials, and
  is not connected to any service operated by the author.
- The author of this app cannot view, recover, or revoke your broker
  access. Revoke via your own broker's API-app dashboard if a key leaks.
- Trades are user-initiated: every execution requires you to paste a
  CSV and confirm with `y`. There is no background trading loop.

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
pytest -v          # ~200 tests, a couple of seconds
ruff check msts_trader
```

The test suite covers:

- CSV parser (header validation, weights, leverage, comments, dup/neg guards)
- Diff math (drift threshold, exits, warnings, blockers, BP overrun, leverage)
- Market hours (RTH/pre/after/closed, holidays through 2027, weekends)
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
