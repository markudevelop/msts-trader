# Studio: Composer-style strategies

`msts-trader ui` is a local web app for building, backtesting and deploying
rule-based allocation strategies ("symphonies" in Composer's terms). It adds
no second trading engine. A strategy resolves to a `ticker,weight` CSV, and
the existing `rebalance --sleeve` pipeline trades it, with every guard that
pipeline already has.

```
block tree ──evaluate(daily closes)──▶ weights CSV ──▶ msts-trader rebalance --sleeve <id> --json
```

## Install and run

```bash
pip install "msts-trader[ui]"          # fastapi, uvicorn, yfinance, numpy
msts-trader ui                         # --port 8765 --no-browser --no-scheduler
```

State lives next to the rest of msts-trader's:

| What | Where |
|---|---|
| strategies | `~/.msts-trader/strategies/<id>.json` (one `.bak` per file) |
| run log | `~/.msts-trader/runs.jsonl` |
| price cache | `~/.msts-trader/prices/` (finalized daily closes only) |
| sleeve capital and holdings | `~/.msts-trader/sleeves/` (the existing ledger) |

## Blocks

| Block | Meaning |
|---|---|
| Asset | Hold one ticker |
| Weight · Equal | Split evenly across children |
| Weight · Specified | Fixed fraction per child. Under 100% leaves cash; over 100% is leverage |
| Weight · Inverse volatility | Weight ∝ 1 / stdev of daily returns over N days |
| If / Else | `indicator(ticker, N) <cmp> value` or `<cmp> indicator(ticker2, M)` |
| Filter | Rank children by an indicator and keep the top or bottom N (equal weight) |
| Group | A named container, equal-weighting its children |

An empty branch means cash. When a filter or inverse-vol block ranks a
**group**, it uses the group's own simulated return series over the window,
the same way Composer does.

Indicators follow Composer's names and units. Returns, volatility and
drawdown are in **percent**, so `cumulative-return(SPY, 10) > 5` means +5%.

- `current-price`
- `cumulative-return`
- `moving-average-price`
- `exponential-moving-average-price`
- `moving-average-return`
- `relative-strength-index` (Wilder)
- `standard-deviation-price`
- `standard-deviation-return`
- `max-drawdown`

## Backtest assumptions

- Adjusted daily closes from Yahoo Finance (yfinance). Data quality is
  Yahoo's. Delisted tickers are missing, which gives survivorship bias.
- Weights are decided from closes up to day *t* and held from close *t* to
  close *t+1*. There is no lookahead: `tests/test_symphony.py` pins this
  with a canary strategy that would only win by peeking.
- Live runs trade about 10 minutes before the close, on a near-close price.
  The backtest assumes a fill *at* the close, so it is slightly
  optimistic by that gap, and by any spread or slippage beyond the cost you
  set (bps per unit of turnover, default 5).
- The backtest starts once the youngest ticker has enough history for the
  longest indicator window.
- Rebalance cadence (daily / weekly / monthly / quarterly / yearly) applies
  in the backtest and to scheduled runs. Composer's threshold ("corridor")
  rebalancing is not modeled. The deploy **drift threshold** is what limits
  live turnover.

A good backtest is not evidence of an edge. Treat the numbers as a
description of the past, not a forecast.

## Comparing strategies

On the **Backtest** tab, pick other saved strategies under "Compare with".
They run over their **common window**, starting from the latest warm-up
among them, so every curve starts on the same day. You get the overlaid
growth curves, a side-by-side metrics table with the best value in each
column highlighted, drawdowns, and the correlation of daily returns. A
correlation near 1 means two strategies are close to the same bet.

## Deploying

Each strategy trades as its own **sleeve**. The strategy id is the sleeve
name, and [sleeves](design-strategy-sleeves.md) keep a local tally of the
shares the strategy bought:

- It never trades your manual positions or other strategies' shares.
- **Invest** earmarks capital; no money moves. The sleeve then sizes against
  its own NAV, so gains compound inside it. **Withdraw** reduces it, and the
  next run sells to cover.
- **Preview orders** evaluates on the latest prices (intraday during the
  session) and runs `rebalance --dry-run` for the sleeve.
- **Execute** runs `rebalance --yes`. It requires:
  1. "Allow live orders" on the strategy (a saved setting), and
  2. typing the strategy id in the confirmation dialog on every manual run.

  Paper trading goes through the same path.

Everything the CLI enforces still applies: session hours, idempotency
(identical targets once per day unless forced), margin-aware sizing,
post-trade verify and self-heal, the negative-residual refusal, and so on.

### Live performance (out-of-sample)

Go-live is the strategy's first executed live run (paper counts). From then
on the **Deploy** tab shows:

- **Actual**: the sleeve's own value (cash + holdings) at every close.
  Snapshots of the sleeve are recorded after each live run and each
  invest/withdraw (`~/.msts-trader/sleeve_snapshots.jsonl`), so the history
  is exact without anything running in the evening. Returns are
  time-weighted: adding or withdrawing capital is not performance.
- **Model**: the same strategy backtested over the same days.
- **Gap**: actual minus model, i.e. what execution costs you (spread,
  timing, rounding, missed days).
- The benchmark (SPY), plus $ P&L against contributed capital.

On the **Backtest** tab, the part of the curve after go-live is drawn as
out-of-sample. The earlier part is in-sample, because the rules may have been
shaped by that history.

### Scheduler

While `msts-trader ui` is running, enabled strategies run at their
schedule time on the **New York market clock** on trading days, whatever
timezone the computer is in. For example, 15:50 ET is about 05:50 (AEST) or
06:50–07:50 (AEDT) the next morning in Sydney, and both sides' daylight-saving
changes are handled. The Deploy tab shows the next run in both ET and your
local time. On half-days the run moves to
10 minutes before the early close. Each strategy runs once per rebalance
period, which is recorded in the run log, so restarts never double-run. If
**Allow live orders** is off, a scheduled run is a dry-run preview.

For unattended trading without the UI running, use cron or GitHub Actions:

```bash
50 15 * * 1-5  msts-trader strategy run my-strategy --yes   # TZ=America/New_York
```

## Security model

The server can place orders, so:

- It binds `127.0.0.1` only.
- Every `/api` call needs the random per-process token from the startup URL
  (`?t=…`, moved to `sessionStorage` on load). Other web pages can make your
  browser send requests to localhost, but they can't read or guess the token.
- Requests with a foreign `Origin` header are refused.
- Live runs need the per-strategy opt-in **and** the typed confirmation.
  Both are enforced server-side, not just in the UI.

Keep the startup URL private. Anyone who has it can drive the UI while the
process runs.

## Importing from Composer

Paste the symphony's EDN (from its editor's source view) or JSON into
**Import**, or run `msts-trader strategy import FILE`. Supported:

- `root`, `asset`, `group`, `wt-cash-equal`, `wt-cash-specified`,
  `wt-inverse-vol`, `if`/`if-child` (else-if chains become nested ifs),
  `filter`, `empty`
- all nine indicators above, with `:window-days` or `:fn-params {:window}`

Anything else (another indicator, `:rebalance :none` corridors, …) is
reported with its path. Corridors fall back to daily with a warning;
unknown blocks and indicators stop the import rather than being dropped.

Composer's format is not publicly specified. If a real export fails to
import, please open an issue with it.
