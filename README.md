# msts-trader

Paste a target-weights CSV, preview the rebalance, execute it on your Tastytrade account.

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

Account 5W******  ·  NAV $48,213.42  ·  cash $2,150.00  ·  BP $46,290.00
Market: open  ·  closes in 23 min

           Rebalance preview
┃ Symbol ┃ Current % ┃ Target % ┃   Δ $ ┃ Action                  ┃ Note ┃
┃ SPY    ┃    18.2%  ┃   42.0%  ┃ +$11k ┃ BUY  22.00 @ ~$521.34   ┃      ┃
┃ EEM    ┃    31.5%  ┃   20.0%  ┃  -$5k ┃ SELL 119.00 @ ~$47.21   ┃      ┃
...

Execute 4 orders? [y/N]: y
[1/4] SPY  BUY  22.00 @ MKT ...  ROUTED  id=4f8...
...

Done.  sent: 4  ·  failed: 0  ·  log: ~/.msts-trader/fills/
```

## Install

```bash
pip install msts-trader
```

Python ≥3.11 required.

Install from source (development):

```bash
git clone https://github.com/markudevelop/msts-trader.git
cd msts-trader
pip install -e .
```

## One-time setup

You need Tastytrade OAuth credentials. **This is your app, not ours** — we never see your keys.

1. Sign in at https://developer.tastytrade.com → **My Apps**
2. Create an OAuth application — copy the **provider secret**
3. Run their OAuth authorization flow to obtain a **refresh token**
4. Look up your **account number** in the Tastytrade web dashboard (optional — leave blank to auto-pick your first account)
5. Run:

```bash
msts-trader login
```

Paste the three values when prompted. They are stored in your OS keychain
(macOS Keychain / Windows Credential Manager / libsecret). The app never
writes them to disk in plaintext.

## Daily usage

1. Get your CSV. On a supported weights site, click **Copy CSV**. Or build one yourself:

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
   msts-trader
   ```

3. Paste the CSV, hit `Ctrl+D` (`Ctrl+Z` then Enter on Windows).
4. Review the preview table carefully.
5. Type `y` to execute, anything else to cancel.

### Useful flags

```bash
msts-trader rebalance --dry-run               # preview only, never sends
msts-trader rebalance --yes                   # skip the confirm prompt
msts-trader rebalance --threshold 0.02        # tighter rebalance (default 4%)
msts-trader rebalance --csv-file targets.csv  # read from a file instead of stdin
```

### Other commands

```bash
msts-trader status     # show NAV, positions, market status
msts-trader logout     # clear stored creds
msts-trader --version  # print version
```

## What it does

- Parses your CSV into `{ticker: target_weight}`.
- Pulls live NAV, cash, buying power, and current positions from Tastytrade.
- Quotes every relevant symbol via the Tastytrade market-data API.
- Computes the dollar delta per ticker, skips anything within the drift
  threshold (default 4% of NAV).
- Sells tickers no longer in your targets.
- Sizes buys at the current quote, rounded to 2 decimals (Tastytrade
  fractional shares on MARKET orders).
- Shows the full plan and waits for `y` before sending anything.
- Submits MARKET DAY orders. Logs results to `~/.msts-trader/fills/`.

## What it does NOT do (v1)

- Pre-market or after-hours execution — refuses to send outside 09:30–16:00 ET.
- Shorting — negative weights are rejected.
- Options, futures, crypto.
- Multi-account or per-strategy ledger.
- Margin-aware uniform scaling (warns instead; Tastytrade's own BP
  pre-flight will scale down at submit if needed).
- Automatic CSV polling. You paste each rebalance manually.

## Security

- Your Tastytrade OAuth credentials live only in your OS keychain on your
  own machine. The app does not phone home, does not log credentials, and
  is not connected to any service operated by the author.
- The author of this app cannot view, recover, or revoke your
  Tastytrade access. Revoke via your own Tastytrade OAuth app dashboard
  if a refresh token leaks.
- Trades are user-initiated: every execution requires you to paste a CSV
  and confirm with `y`. There is no background trading loop.

## Disclaimer

This tool sends real orders to your live brokerage account. You are
responsible for the CSV you paste and the rebalance you confirm. Past
performance of any signal source is not indicative of future results.
The author makes no warranty of any kind; use at your own risk.

## License

Apache-2.0.
