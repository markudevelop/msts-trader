# Design: strategy sleeves (per-strategy share tally in one account)

Status: **P1 implemented** (`rebalance --sleeve`, the ledger in
`msts_trader/sleeves.py`, the `sleeve` command group, fill-driven settlement,
the residual gate, sleeve-scoped verify/self-heal). P2 (sleeve-scoped stops,
`liquidate --sleeve`) and P3 (broker-side tags, `multi` integration) are not
built; sleeve runs are market-order-only and refuse stops. The zero-code
workarounds in the README remain valid alternatives.

## Problem

msts-trader reads positions at the **account** level (`diff.py` sizes every
order against `positions.get(ticker)`), so two strategies — or a strategy and
the owner's manual trades — sharing a ticker trade against each other: each run
sees the whole account position as its own drift.

The user story that motivates native support, quoted from the request:

> "I want to try that algo trading thing without much legwork of setting up a
> separate account, so everything works within my existing account alongside my
> manual trades (which I will continue to do). I likely will need to play with
> many different things before I find something which works. And when I find
> it, I want to scale in, while enjoying cross-margining — you can only have so
> many PM accounts."

Separate accounts fail this story twice: they don't scale across experiments,
and they forfeit cross-margin. The disjoint-ticker recipe (`--allocation` +
`--no-sweep`) works but is operator-enforced — nothing detects a collision.
Native support means: a strategy may hold a ticker the owner also holds, and
the tool knows exactly how many of the shares are the strategy's.

## Core model

**Every order the tool sends is tagged with the sleeve that sent it, and a
local ledger accumulates each sleeve's confirmed fills into a per-ticker share
tally.** Sizing then reads the sleeve's tally instead of the account position.

The invariant is deliberately an inequality:

```
for every ticker:   Σ (sleeve tallies)  ≤  account position
```

The gap is **unassigned** — shares the tool never bought. The owner's manual
book is unassigned *by construction*: they declare nothing, maintain no
reserve list, and the tool structurally cannot touch shares it didn't buy.
This is what makes the feature safe alongside manual trading, and it is why
equality must never be asserted: shares move with no tagged order behind them
(manual fills, splits, spin-offs, mergers, DRIP, transfers).

### Why the tag lives locally, not at the broker

Audited against the installed SDKs: Alpaca has `client_order_id`, IBKR
`orderRef`, Tradier a `tag` param, Hyperliquid a `cloid` (128-bit hex, not a
label). **Tastytrade and Schwab expose no reliable free-text tag**, and no
adapter sets one today. A broker-side tag therefore cannot be the source of
truth across all seven brokers — the ledger is local state either way. Where a
broker field exists, set it anyway (best-effort audit trail, and it lets a
future `rebuild` command reconstruct the ledger from broker history on the
brokers that support it), but nothing may depend on it.

## Data

One ledger file per (broker, account), next to the existing state:

```
~/.msts-trader/sleeves/<broker>_<account_id>.json
```

```json
{
  "version": 1,
  "broker": "tastytrade",
  "account": "5W12345",
  "sleeves": {
    "momo":  { "SPY": "31.00", "GLD": "12.50" },
    "carry": { "SPY": "44.00", "SHV": "800.00" }
  },
  "stop_orders": {
    "momo": { "SPY": ["4f8abc..."] }
  },
  "pending": [
    { "order_id": "9d1...", "sleeve": "momo", "ticker": "SPY",
      "side": "BUY", "requested": "22.00", "recorded_fill": "0",
      "ts": "2026-08-29T20:12:00Z" }
  ]
}
```

Rules:

- Quantities are **strings parsed to `Decimal`** — the engine is Decimal
  end-to-end and float drift breaks tally-vs-position comparison at 0.01-share
  granularity.
- All writes go through an `O_EXCL` lockfile with bounded wait — the exact
  pattern already in `runstate.record()` (`runstate.py:53`). The lock is held
  for the whole rebalance run, which also serializes two sleeve runs against
  the same account.
- Write via temp-file + atomic rename; keep one `.bak` of the previous state.
  This file is money-adjacent: a torn write must not be able to destroy the
  tally.
- `recorded_fill` on a pending entry makes settlement idempotent: on each
  later observation, apply only `filled_qty - recorded_fill`, then advance
  `recorded_fill`. A crash between "apply" and "advance" (single JSON write —
  can't happen separately) or a re-poll never double-counts.

## Write path — fills only, never intent

Hook: `_execute()` (`__main__.py:1783`), which already polls
`broker.order_status(order_id)` after sending. The Broker Protocol requires
`order_status → {status, filled_qty, filled_avg_price}` and **all seven
adapters implement it** — that is the ledger's sole write source.

- Apply `± filled_qty` (not the ordered quantity) to `sleeves[name][ticker]`.
  Writing intent corrupts the tally on the first partial fill or reject.
- Chase orders: `chase_fill()` returns `filled_quantity` in its result dict
  (note the different key from `order_status`'s `filled_qty`); the write path
  must read both shapes.
- An order that is not terminal after the post-send poll (`RESTING`/`WORKING`)
  goes into `pending` with `recorded_fill` = whatever was confirmed so far.
  Every subsequent run (any sleeve — settlement is account-scoped) first
  settles `pending` via `order_status` before computing anything.
- A sleeve tally clamps at 0 on the sell side: a broker over-reporting
  `filled_qty` must never drive a tally negative (mirror of the chase
  engine's monotonic-fill clamp).

## Read path — sizing

`build_preview()` grows one optional argument: the sleeve's owned quantities.
`diff.py:79` (`cur = positions.get(t.ticker)`) is the **only** place ownership
enters sizing; when sleeve quantities are supplied, build the `Position` from
`min(tally, account_qty)` (valued at the live quote, as `_mv` already does).
Downstream — drift, whole-book trigger, exits, margin scaling — is untouched
because the `Preview` shape is identical.

- **Sells clamp to `min(tally, account_position)`.** Never sell shares the
  ledger claims but the account lacks.
- **The sweep becomes sleeve-scoped**: "held but not in targets" means
  *tally-owned* but not in targets. Unassigned shares and other sleeves'
  shares are invisible to the sweep, so a sleeve run keeps the default sweep
  semantics safely — `--no-sweep` stops being necessary for sleeve runs.
- `--allocation` stays orthogonal (it sets the dollar base); a sleeve run
  should still require it or a config `allocation`, since "fraction of whole
  NAV" is rarely what a sleeve means.
- Default `--threshold-mode position` for sleeve runs — a small sleeve's lines
  rarely move 4% of account NAV (same reasoning already documented for the
  merged-book recipe).

## Stops

Today `_reconcile_stops` sizes every stop to `held.quantity` — the full
account holding (`__main__.py:2176-2183`) — and its orphan sweep cancels any
open stop with no live position. Both must become sleeve-scoped:

- Stop quantity = the sleeve's tally for that ticker, not the account holding.
  Otherwise a sleeve's stop covers the owner's manual shares, and a triggered
  stop sells them.
- Record placed stop order-ids under `stop_orders[sleeve][ticker]`. The orphan
  sweep and pre-cancel-before-sell then only touch stops the *current* sleeve
  owns — otherwise sleeve B's run cancels sleeve A's protection.
- A stop that fires between runs is a broker-side sell with no run watching
  it. It surfaces as a negative residual on the next run (see below) unless
  the settlement pass finds the stop order-id filled — so settle
  `stop_orders` through `order_status` exactly like `pending`.

## Reconciliation — fail closed

Each run, after settling `pending`, compute per ticker:

```
residual = account_position − Σ all sleeve tallies
```

| residual | meaning | action |
|---|---|---|
| `> 0` | unassigned shares (manual book, dividends, splits) | leave untouched — this is normal, not an error |
| `= 0` | ledger agrees with broker | proceed |
| `< 0` | tallies claim more than exists | **refuse to run** |

Negative residual means algo shares vanished outside the tool — a manual sell
of sleeve shares, an unsettled corporate action, a stop fill the settlement
pass couldn't attribute. The tool cannot know *whose* shares vanished, and any
auto-heal (pro-rata write-down, oldest-first) is a guess that silently
mis-sizes a real order. Push a specific message into `preview.blockers`; the
existing refusal path (`__main__.py:1594`) stops the run before any order.
The operator fixes it explicitly:

```
msts-trader sleeve reconcile            # shows per-ticker tally vs account, proposes fixes
msts-trader sleeve adjust momo SPY 44   # operator asserts the true tally
```

Splits/spin-offs are a special case of the same rule: the account quantity
jumps with no order. A 2:1 split shows as residual `> 0` (harmless but the
sleeve is now under-claiming; its next run would buy) — `sleeve adjust` is the
manual fix in v1; automatic corporate-action handling is explicitly out of
scope.

## Cash: a sleeve manages its own money

(Shipped after P1, prompted by user feedback: a static `--allocation` is
constant-dollar rebalancing — it trims every gain back to the fixed figure and
buys a drawdown back up with the ACCOUNT's money. Reproduced on paper before
fixing: SPY doubling was trimmed 40→20 shares; SPY halving was topped up
20→80 with $15k of account cash.)

`sleeve invest NAME AMOUNT` adds virtual capital; `divest` takes it back out
of the sleeve's cash — which may go negative; the next rebalance sells
holdings down to the reduced NAV to cover it, bounded only by the sleeve's
NAV (you cannot take out more than it is worth). Nothing moves at the broker — the
point of sleeves is that all dollars stay in one cross-margined account. From
the first invest, the sleeve is cash-tracked:

- Sizing base = the sleeve's own NAV (cash + holdings at live quotes),
  recomputed each run and again post-fill for verify/self-heal.
- Confirmed fills move cash exactly: a cost cursor (`recorded_cost`) applies
  `filled * filled_avg_price - recorded_cost` per settlement, which is exact
  across partial fills at different prices because the broker average is
  cumulative; `est_price` recorded at placement is the fallback for adapters
  whose order_status has no average.
- `--allocation` on a cash-tracked sleeve is refused (the trap above);
  legacy 0.28.0 sleeves (no cash record) keep it with a warning.
- A leveraged sleeve (weights sum > 1) runs its cash negative by design —
  that is the cross-margin borrow, bounded account-wide by margin-aware
  sizing.
- NAV ≤ 0 refuses to run (a zero-capital sleeve must not fall back to
  account NAV sizing).

## CLI surface

```bash
msts-trader rebalance --sleeve momo --allocation 50000 --csv-file momo.csv
msts-trader liquidate --sleeve momo            # flatten ONLY the sleeve's tally

msts-trader sleeve list                        # sleeves, tickers, tallies, residuals
msts-trader sleeve show momo
msts-trader sleeve adopt momo SPY 100          # assign already-held shares (day-one bootstrap)
msts-trader sleeve release momo SPY 100        # tally -> unassigned (tool stops managing them)
msts-trader sleeve adjust momo SPY 44          # assert a tally during reconciliation
msts-trader sleeve reconcile [--broker ...]
```

- `adopt` is load-bearing: on day one every share is unassigned, so migrating
  an existing book into a sleeve must be explicit. `adopt` must refuse to
  push `Σ tallies` past the account position.
- Without `--sleeve`, behaviour is exactly today's (account-level, whole
  book). No ledger is read or written. **Zero behaviour change for existing
  users** — a sleeve ledger with entries plus a no-`--sleeve` rebalance on
  the same account is refused (blocker), since an account-level sweep would
  sell every sleeve's holdings.
- Idempotency: add `sleeve` to the fingerprint params in
  `runstate.fingerprint()` — two different sleeves running the same targets
  the same day must not suppress each other.
- `multi`: a `sleeve = "..."` key per `[[account]]` row composes naturally
  (several sleeves in one account = several rows sharing creds, differing by
  `sleeve`), but ships after single-account sleeves are proven.

## Interactions with existing machinery

- **verify/self-heal** re-run the same diff, so they inherit sleeve scoping
  for free once `build_preview` takes owned quantities — but self-heal's
  re-execution must route through the same tagged write path so healed fills
  land in the tally.
- **Margin-aware sizing** still reads account-wide buying power. Sleeves
  compete for BP first-come-first-served; per-sleeve BP budgeting is out of
  scope (document it).
- **NAV in `--json`/notifications** stays account NAV; sleeve dollar value is
  derivable from the tally and quotes and belongs in `sleeve show`.

## Failure modes considered

| event | handling |
|---|---|
| partial fill | `filled_qty` applied; remainder stays pending |
| reject | `filled_qty = 0`; nothing written |
| resting order across runs | settled by the pre-run settlement pass, idempotent via `recorded_fill` |
| crash mid-run | ledger written per-order after confirmation; pending entries settle next run; lockfile has bounded wait + stale-lock note in error |
| manual sell of sleeve shares | negative residual → refuse + `sleeve reconcile` |
| manual buy of a sleeve ticker | residual `> 0` → unassigned, untouched |
| stop fired between runs | settlement of recorded stop order-ids; else negative residual → refuse |
| split / spin-off / DRIP | residual change → unassigned (positive) or refuse (negative); `sleeve adjust` |
| two sleeve runs concurrently | ledger lock serializes them |
| broker over-reports fills | tally clamped; sell sizing clamps to account position |

## Testing plan

Money-moving state — the test burden is the majority of the cost:

1. Ledger unit tests: Decimal round-trip, atomic write/backup, lock
   contention, idempotent settlement (`recorded_fill` advance), adopt/release
   bounds.
2. `build_preview` with owned quantities: shared-ticker independence (two
   sleeves + manual shares in SPY; each run touches only its own), sell
   clamp, sleeve-scoped sweep, exits.
3. Stop scoping: quantity = tally, orphan sweep ignores other sleeves' stops,
   pre-cancel only own stops.
4. Reconciliation: all residual cases in the table above, fail-closed
   blocker text.
5. End-to-end on the paper broker: two sleeves + manual position, multi-run
   convergence, a resting order settling one run later, mutation-style checks
   (intent-vs-fill, drop-pending, unclamped sell must each fail the suite).
6. The no-`--sleeve` guard on a ledgered account.

## Phasing

1. **P1 — ledger + rebalance:** ledger module, tagged write path in
   `_execute`, sleeve-scoped `build_preview`, settlement pass, residual gate,
   `sleeve list/show/adopt/adjust`, fingerprint param. Ships usable.
2. **P2 — stops + liquidate:** sleeve-scoped stop sizing/ownership,
   `liquidate --sleeve`, `sleeve release/reconcile` UX.
3. **P3 — conveniences:** broker-side best-effort tags, `multi` integration,
   ledger `rebuild` from broker history where tags exist.

Out of scope (all phases): per-sleeve buying-power budgets, automatic
corporate-action handling, per-sleeve P&L reporting (the fill log already
carries enough to compute it externally), short sleeves.
