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

## Home and library

Studio runs use each strategy's own settings: its order type, threshold,
fractional or whole shares (whole only for market-on-close or brokers that
can't trade fractions) and no minimum weight. Defaults such as `whole_shares`,
`min_weight` or `moc` in `~/.msts-trader/config.toml` apply to plain CLI
`rebalance` runs only.

Home has these tabs:
- **Live**: funded strategies trading real money at a broker.
- **Incubation**: funded strategies on the `paper` broker, on a real broker's
  paper / sandbox account (tick *This is a paper / sandbox account* in Deploy,
  e.g. Alpaca paper or the Tradier sandbox), or with live orders off
  (preview-only). Use it to paper-trade many strategies without crowding Live.
- **Pinned tags**: press **+** to pin any tag as its own tab (e.g. *Options*,
  *Active strategies*, *Passive weights*); **×** unpins it, and the tag stays on
  its strategies. A tag tab lists its funded strategies with their stats, then
  the unfunded ones.
- **All strategies**: the whole library.

Live and Incubation each have their own **Combined portfolio**, so paper
positions never inflate the real-money totals. Each funded tab shows:
- capital, and current value (cash + holdings at the latest cached close)
- **held / target** positions. When the latest run left targets unbought it
  shows *N not bought*; hover for each ticker and the rebalance engine's
  reason, e.g. "qty rounds to 0 (whole-share)" for market-on-close orders
  worth less than one share (switch the order type to Market or add capital).
- schedule
- the **last check**: the last scheduled or manual rebalance check, labelled
  *preview only* when "Allow live orders" is off, so no orders were placed
- **Live**: the sleeve's own out-of-sample record since go-live (the first
  executed live or paper run). Shows *Since* (the OOS start date), CAGR, max
  drawdown and Sharpe from the time-weighted sleeve index, so deposits and
  withdrawals are not returns. Under 30 days live it shows the total return
  instead of an annualised CAGR or Sharpe. It is computed from cached closes
  only; a ⚠ means a held ticker has no cached price yet (opening the strategy's
  Performance tab fetches it).
- **Backtest**: the CAGR / max drawdown / Sharpe from each strategy's last
  full-history backtest

Below it, the **Combined portfolio** sums every funded strategy's latest
target, times its capital, by ticker, next to what the sleeves actually hold. That's saved whenever you run one, so
nothing is recomputed daily. **All strategies** is the whole library as a
compact table. You can search, filter by tag, sort, and select rows for bulk
actions:
- tag / untag
- pause / resume schedules
- export JSON (a zip)
- delete
- **go to cash**: sells everything the strategy's sleeve holds and pauses it.
  It needs a typed `CASH` confirmation; real orders on real brokers.

Tags are edited under each strategy's name. The sidebar lists the Live
(real-money) strategies and the 20 most recently opened ones, plus a search box
for everything else.

## Blocks

| Block | Meaning |
|---|---|
| Asset | Hold one ticker |
| Weight · Equal | Split evenly across children |
| Weight · Specified | Fixed fraction per child. Under 100% leaves cash; over 100% is leverage |
| Weight · Inverse volatility | Weight ∝ 1 / stdev of daily returns over N days |
| If / Else | `indicator(ticker, N) <cmp> value` or `<cmp> indicator(ticker2, M)`, or ALL of / ANY of several such comparisons (nestable) |
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

### Combining strategies

Below the comparison, **Combine into one strategy** turns the strategies you
compared into a blend. Give each one a share (for example 50% / 50%), then:

- **Backtest blend** adds it to the comparison, so it's measured against its
  parts on the same days.
- **Save as strategy** stores it as a normal strategy you can edit, deploy,
  schedule and track.

A blend is the parts run side by side in one sleeve. Each day it holds
share × what each part would hold, and rebalances back to the shares. It
rebalances at the most frequent cadence among its parts (a monthly part in a
daily blend is re-evaluated daily) unless you pick one. Shares under 100%
leave cash; over 100% is leverage. Tickers that several parts hold are netted
into one position, so a blend trades less than running each part in its own
sleeve.

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

### Sleeve tools

The **Sleeve tools** panel (Deploy tab, under Capital) runs the
`msts-trader sleeve` bookkeeping commands for the strategy's sleeve. None of
them place orders.

| Panel control | CLI command | What it does |
|---|---|---|
| Reconcile account | `sleeve reconcile` | Settles pending orders, then shows every ticker's account shares, sleeve claims, and unassigned shares for the whole account. A negative "Unassigned" blocks the next run until fixed. |
| Adopt held shares | `sleeve adopt` | Gives shares you already hold, and no sleeve owns, to this strategy. |
| Release shares | `sleeve release` | Returns shares from the strategy to your manual book. |
| Set tally | `sleeve adjust` | Overwrites the strategy's share count for one ticker (asks to confirm). |
| Base | `sleeve base` | Sizes against its own NAV (the default, compounding), a % of account NAV, or fixed dollars. |
| Cap | `sleeve cap` | The most the strategy may deploy, in dollars or % of account NAV, or no cap. |

The panel shows the current base and cap. Adopt, release and set tally also
record a snapshot for the live-performance chart.

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

### Running schedules without Studio open

Deploy → Automation → **Install** registers one OS task: Windows Task
Scheduler (runs while you're logged in, using `pythonw` so no window
flashes), or a crontab line on macOS/Linux. Every minute it runs
`msts-trader strategy run-due`, which applies exactly the scheduler's rules.
It holds a shared lock (`~/.msts-trader/scheduler.lock`) and checks the
once-per-period stamp, so it and an open Studio never run the same strategy
twice. **Remove** uninstalls it. The CLI equivalent is
`msts-trader strategy schedule install|uninstall|status`.

### Notifications

Sidebar → **Notifications**: a webhook URL (Discord, Slack or any JSON
endpoint) and/or a Telegram bot token plus chat id. Secrets go to the OS
keychain. Choose to be notified after live runs and errors (the default),
every run, or never. The message names the strategy and lists its orders, or
the error. The optional **weekly digest** goes out on Fridays after the close
and lists each funded strategy's capital, value, P&L and last run.

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

**Order type** (Deploy tab) is **Market** by default: orders fill when the run
executes. **Market-on-close** (Alpaca, IBKR, Schwab, paper) fills in the
closing auction instead. Exchanges stop accepting MOC orders around 15:50 ET,
so a MOC strategy runs no later than 15 minutes before the close (15:45 ET,
12:45 on half-days) even if its schedule time is later. Studio always passes
the strategy's own order type, so `moc = true` in `config.toml` doesn't affect
Studio strategies. pnlportfolio books publish near 15:45 ET, so use Market for
them.

**Limit chase** (all brokers) works each order as a limit at the mid,
repriced a few times, then sends a market order for anything still unfilled,
so you pay less of the spread. It takes about 30 seconds per order, so a
scheduled chase strategy also runs no later than 15:45 ET (12:45 on
half-days). Studio always finishes each order with the market fallback.

**Extended hours** (every broker except Hyperliquid) is a limit-only chase
that may also run premarket and after-hours. The scheduled time can be any
time from 04:00 to 19:50 ET on trading days, half-days included. Anything
still unfilled after the last reprice is cancelled, not sent at market, and
the run is reported as incomplete. Your broker's own extended session,
symbol eligibility and account permissions still apply. With Market,
Market-on-close or Limit chase, a schedule outside 09:30–16:00 ET is flagged
on the Deploy tab, because those orders would be refused.

**Execution** (Deploy tab) holds the per-strategy versions of the
`rebalance` options. Studio always passes them, so `config.toml` doesn't
change Studio strategies:

| Setting | `rebalance` flag | Default |
|---|---|---|
| Rebalance scope: whole book / per ticker | `--rebalance-scope` | whole book |
| Minimum weight | `--min-weight` | 0 (trade every target) |
| Max buys per run | `--max-notional` | no cap |
| Whole shares only | `--whole-shares` | off (always on for MOC) |
| Chase reprices, seconds per reprice, aggression | `--chase-retries`, `--chase-interval`, `--chase-aggression` | 5, 5 s, 0 (blank uses `config.toml` or these defaults) |

Each strategy sends its own orders; Studio does not net them across
strategies. IBKR is reported to reject a MOC order on a ticker that already
has an open MOC order on the other side, and the first side wins. If two MOC
strategies share an IBKR account and one buys a ticker while the other sells
it, one leg can be rejected. Avoid overlapping tickers between MOC
strategies on one account, or set one of them to Market.

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

## Importing pnlportfolio.com books

**Import → pnlportfolio books** lists every runnable book from the public
catalog. The Research desk books (Core, Apex, Hydra, Fusion, Unified) are
listed first and pre-selected. Paste your API token once; it goes to the OS
keychain and is never written into strategy files or sent back to the page.
Headless runs can use `PNLPORTFOLIO_TOKEN` instead.

Each book becomes a strategy with one **Feed** block:

- **Live runs** fetch the book's current target weights
  (`/v1/sleeves/<book>/weights`). The API refuses a book that isn't current
  yet (pnlportfolio publishes near 15:45 ET), so the default 15:50 ET schedule
  runs just after publication. Books must be long-only. Protective stops a
  book publishes are not placed, because sleeves don't place stops yet; the
  run log says so.
- **Backtests** use the book's own published daily NAV
  (`/v1/sleeves/<book>/nav.csv`). This is pnlportfolio's published history,
  and most of it is their backtest rather than live results. The published NAV
  can lag the live book by weeks; backtests then end on its last day.
- A Feed is an ordinary block. Add one anywhere ("+ Add block → Feed"),
  blend books with your own strategies, or rank books in a Filter by momentum.

CLI equivalents: `msts-trader strategy feeds`, `strategy feed-token`,
`strategy import-feed core apex hydra blend unified`.

## Custom feeds (any URL)

**Import → Custom feed** turns any URL that publishes target weights into a
strategy, for example a Google Sheet published as CSV, a raw GitHub file, your
own script, or another weights API.

| Field | |
|---|---|
| Weights URL | `ticker,weight` CSV (msts-trader's own format; `# asof:` honoured) or JSON: `{"weights": {"SPY": 0.4, ...}}`, or a flat `{"SPY": 0.4, ...}` |
| History URL | optional `date,<value>` CSV. The value column is `nav_net` / `nav` / `value` / `equity` / `close`, else the last column |
| Token | none, `Authorization: Bearer <token>`, or a query parameter (`?token=`, name configurable). Stored in the OS keychain for this feed |

**Test** fetches both URLs and shows positions, gross exposure, top holdings
and the history range, without saving anything. **Import** creates a
strategy holding one Feed block. The block stores the URLs and how to send the
token, never the token itself.

- **Without a history URL**, Studio records the feed's weights each day it
  evaluates it (`~/.msts-trader/feed_history/`) and rebuilds a daily NAV from
  real closes. Backtests and ranking start once a few days are recorded. Live
  runs work from day one.
- **Freshness**: weights stamped more than 6 days old are refused, which
  clears weekends and holidays for a daily feed. For JSON, the newest of
  `asof` / `as_of` / `date` / `trade_date` / `updated` counts. Unstamped feeds
  are trusted.
- Same rules as every feed: long-only, and protective stops in the feed are
  reported, not placed. There's a 5 MB limit and http(s) only.

CLI: `msts-trader strategy import-url "My model" https://…/weights.csv --nav-url https://…/nav.csv --auth bearer --token-prompt`.

pnlportfolio books are this same block with a built-in preset (catalog
picker, stale-book messages). A pnlportfolio API URL also works as a plain
custom feed.

## Importing from Composer

Paste the symphony's EDN (from its editor's source view) or JSON into
**Import**, or run `msts-trader strategy import FILE`. Supported:

- `root`, `asset`, `group`, `wt-cash-equal`, `wt-cash-specified`,
  `wt-inverse-vol`, `if`/`if-child` (else-if chains become nested ifs),
  `filter`, `empty`
- all nine indicators above, with `:window-days` or `:fn-params {:window}`

Conditions from Composer's newer editor (the structured `condition`: binary,
multi-ticker `%` conditions, any/all compounds) import as ALL of / ANY of
groups and take precedence over the stale `lhs-`/`rhs-` fields edited nodes
keep. Anything else (another indicator, `:rebalance :none` corridors, …) is
reported with its path. Corridors fall back to daily with a warning;
unknown blocks and indicators stop the import rather than being dropped.

Composer's format is not publicly specified. If a real export fails to
import, please open an issue with it.
