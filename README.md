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

Done.  sent: 4  ·  failed: 0  ·  log: ~/.msts-trader/fills/
```

## Supported brokers

| Broker     | Status                  | Auth                      | Notes |
|------------|-------------------------|---------------------------|-------|
| Paper      | shipped, tested         | local file                | $100k starting cash, no real fills, 14 unit tests |
| Tastytrade | shipped, **live-tested** | OAuth refresh token       | indefinite token, BYO OAuth app — connect / balances / positions / quotes / dry-run all confirmed against a real account |
| Alpaca     | shipped, **live-tested** | API key + secret          | paper or live, fractional supported — end-to-end confirmed on a paper account |
| IBKR       | shipped, beta            | TWS / IB Gateway socket   | `pip install "msts-trader[ibkr]"`, works with local or Dockerised Gateway, **awaiting live-fill confirmation** |
| Schwab     | shipped, beta            | OAuth2 + browser callback | `pip install "msts-trader[schwab]"`, 7-day refresh, **awaiting live-fill confirmation** |

**Beta status:** IBKR and Schwab adapters pass structural protocol
conformance tests in CI (signatures, attributes, error handling) but
have not yet been verified end-to-end against a real brokerage account
by the author. Try them in paper mode first, or file an issue with a
fill report if you run them live.

Open a GitHub issue if you want one prioritised.

## Install

```bash
pip install msts-trader
```

Python ≥3.11 required.

### Optional brokers

IBKR and Schwab require extra dependencies. Install them only if you
plan to use that broker:

```bash
pip install "msts-trader[ibkr]"      # adds ib_insync + nest_asyncio
pip install "msts-trader[schwab]"    # adds schwab-py
pip install "msts-trader[all]"       # everything
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

   - `weight` is a fraction (0–1), not a percent.
   - Sum should be ≤ 1.0 (the remainder is held as cash).
   - Comments starting with `#` are ignored.

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

### Other commands

```bash
msts-trader status                  # NAV, positions, market status (default broker)
msts-trader --broker alpaca status  # other broker
msts-trader brokers                 # list supported + configured brokers
msts-trader logout --broker alpaca  # clear stored creds for one broker
msts-trader paper-reset             # reset paper book to starting cash
msts-trader --version
```

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

## What it does NOT do (v0.2)

- Pre-market or after-hours execution. Refuses outside 09:30–16:00 ET.
- Shorting. Negative weights are rejected.
- Options, futures, crypto.
- Multi-account or per-strategy ledger.
- Active stop management (Hydra/Fusion-style watchers).
- Automatic CSV polling. You paste each rebalance manually.

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

## Development

```bash
git clone https://github.com/markudevelop/msts-trader.git
cd msts-trader
pip install -e ".[all,dev]"
pytest -v          # 95 tests, ~2 seconds
ruff check msts_trader
```

The test suite covers:

- CSV parser (header validation, weights, comments, dup/neg/>1 guards)
- Diff math (drift threshold, exits, warnings, blockers, BP overrun)
- Market hours (RTH/pre/after/closed, holidays through 2027, weekends)
- Paper broker end-to-end (cash accounting, position lifecycle, dry-run, persistence)
- Broker protocol conformance (every adapter exposes the required attrs + methods)
- Keychain (save/load/clear, default broker, broker enumeration)
- CLI (help, version, brokers list, paper login, no-creds clean exit)

Live brokerage adapters (Tastytrade, Alpaca, IBKR, Schwab) are not
exercised against real APIs in CI — they need credentials and can
move real money. The tests verify structure; you verify fills.

## License

[PolyForm Noncommercial License 1.0.0](LICENSE).

You may use, modify, and share this software for any **noncommercial
purpose** — personal trading, research, education, hobby projects.
**Selling, hosting as a paid service, or otherwise commercializing
this software or derivative works is not permitted** without a separate
commercial license. Contact the author if you need one.
