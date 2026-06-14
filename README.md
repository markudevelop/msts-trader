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
browser window, you authorize, and the token JSON is written to
`~/.msts-trader/schwab_token.json`. Schwab refresh tokens expire every
7 days — re-run `msts-trader login --broker schwab` when that happens.

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
- **`--min-weight`:** rows with `0 < weight < min-weight` are ignored
  entirely — no buy, and an existing position in that ticker is *not*
  exit-swept either. An explicit weight of `0` still means "sell it all".
  Useful when the CSV carries many tiny weights you don't want to trade.
- **`--allocation`:** size the weights against a fixed dollar amount
  instead of the whole account — e.g. run a $50k strategy sleeve inside
  a $200k account. Positions in tickers *not* in the CSV are still
  exited (the sweep is account-wide), so keep sleeve and non-sleeve
  tickers disjoint or rebalance with a CSV that lists everything you
  hold. Capped at NAV; use leveraged weights (sum > 1.0) for gross
  exposure above the allocation.

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
margin_aware = true   # default; set false to disable buying-power-fit scaling
moc = false           # set true to always use market-on-close orders
min_weight = 0.01     # ignore CSV rows with weight under 1%
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
