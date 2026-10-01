// Mirrors msts_trader/symphony/model.py (the server validates everything).

export type IndicatorFn =
  | "current-price"
  | "cumulative-return"
  | "moving-average-price"
  | "exponential-moving-average-price"
  | "moving-average-return"
  | "relative-strength-index"
  | "standard-deviation-price"
  | "standard-deviation-return"
  | "max-drawdown";

export type Comparator = "gt" | "gte" | "lt" | "lte";
export type Cadence = "daily" | "weekly" | "monthly" | "quarterly" | "yearly";

export interface Metric {
  fn: IndicatorFn;
  ticker: string;
  window: number;
}

/** A comparison (lhs/comparator/rhs|rhs_value) or a compound: any (OR) / all (AND). */
export interface Condition {
  lhs?: Metric | null;
  comparator?: Comparator | null;
  rhs?: Metric | null;
  rhs_value?: number | null;
  any?: Condition[] | null;
  all?: Condition[] | null;
}

interface Base {
  weight?: number | null;
}
export interface AssetNode extends Base {
  step: "asset";
  ticker: string;
  name?: string | null;
}
export interface GroupNode extends Base {
  step: "group";
  name: string;
  children: Node[];
}
export interface EqualNode extends Base {
  step: "wt-cash-equal";
  children: Node[];
}
export interface SpecifiedNode extends Base {
  step: "wt-cash-specified";
  children: Node[];
}
export interface InvVolNode extends Base {
  step: "wt-inverse-vol";
  window: number;
  children: Node[];
}
export interface IfNode extends Base {
  step: "if";
  condition: Condition;
  then: Node[];
  else: Node[];
}
export interface FilterNode extends Base {
  step: "filter";
  sort_fn: IndicatorFn;
  window: number;
  select: "top" | "bottom";
  n: number;
  children: Node[];
}

export interface FeedNode extends Base {
  step: "feed";
  provider?: "pnlportfolio" | "url";
  book?: string | null;
  name?: string | null;
  weights_url?: string | null;
  nav_url?: string | null;
  auth?: "none" | "bearer" | "query";
  token_param?: string;
}

export type Node = AssetNode | GroupNode | EqualNode | SpecifiedNode | InvVolNode | IfNode | FilterNode | FeedNode;
export type Step = Node["step"];

export interface Deploy {
  broker: string;
  account: string | null;
  live_enabled: boolean;
  schedule_enabled: boolean;
  schedule_time: string;
  threshold: number;
}

export interface Strategy {
  id: string;
  name: string;
  description: string;
  rebalance: Cadence;
  children: Node[];
  deploy: Deploy;
  tags?: string[];
}

export interface StrategySummary {
  id: string;
  name: string;
  description: string;
  rebalance: Cadence;
  tickers: string[];
  deploy: Deploy;
}

export interface Meta {
  version: string;
  brokers: string[];
  indicators: IndicatorFn[];
  market: { status: string; minutes_to_close: number | null };
}

export interface EvalResult {
  asof: string;
  weights: Record<string, number>;
  csv: string;
}

export interface Metrics {
  cagr: number | null;
  vol: number | null;
  sharpe: number | null;
  max_drawdown: number | null;
  total_return: number | null;
  annual_turnover?: number;
}

export interface BacktestResult {
  start: string;
  end: string;
  dates: string[];
  equity: number[];
  metrics: Metrics;
  allocations: { date: string; weights: Record<string, number> }[];
  cost_bps: number;
  benchmark?: { ticker: string; equity: number[]; metrics: Metrics };
  oos_start?: string | null;
}

export interface CompareResult {
  start: string;
  end: string;
  dates: string[];
  series: { id: string; name: string; equity: number[]; metrics: Metrics; rebalances: number }[];
  correlation: number[][];
  benchmark?: { ticker: string; equity: number[]; metrics: Metrics };
}

export interface OosResult {
  live_since: string | null;
  reason?: string;
  dates?: string[];
  actual?: { nav: number[]; contributed: (number | null)[]; index: number[] };
  model?: { index: (number | null)[] } | null;
  model_error?: string;
  benchmark?: { ticker: string; index: number[] };
  metrics?: {
    actual_return: number | null;
    model_return?: number | null;
    benchmark_return?: number | null;
    tracking_gap?: number;
    pnl: number | null;
    nav: number | null;
    actual_max_drawdown: number | null;
  };
}

export interface PreviewOrder {
  ticker: string;
  side: "BUY" | "SELL";
  quantity: string;
  estimated_price: string | null;
  notional: string;
}

export interface RunEntry {
  ts: string;
  strategy: string;
  mode: "dry" | "live";
  source: string;
  broker: string;
  status: string;
  error?: string;
  asof?: string;
  weights?: Record<string, number>;
  preview?: {
    nav: string;
    cash: string;
    orders: PreviewOrder[];
    warnings: string[];
    blockers: string[];
    duplicate_today: boolean;
  } | null;
  execution?: { sent: number; failed: number; verify?: { converged: boolean } };
}

export interface SleeveLedger {
  account: string;
  cash: string | null;
  contributed: string | null;
  holdings: Record<string, string>;
  pending: { ticker: string; side: string; requested: string; order_id: string }[];
}

export interface SchedulerState {
  running: boolean;
  market_now?: string;
  local_tz?: string;
  local_now?: string;
  last_tick?: string | null;
  last_error?: string | null;
  running_now?: string | null;
  upcoming?: { strategy: string; mode: string; next_check: string; cadence: string }[];
}

export interface FeedBook {
  id: string;
  label: string;
  cagr: number | null;
  sharpe: number | null;
  num_positions: number | null;
  date: string | null;
  featured: boolean;
}

export interface FeedCatalog {
  provider: string;
  has_token: boolean;
  books: FeedBook[];
}

export interface UrlFeedTest {
  positions: number;
  gross: number;
  top: [string, number][];
  asof: string | null;
  stops: number;
  history: { start: string; end: string; days: number } | null;
  history_error?: string;
  token_stored: boolean;
}

export interface DashRow {
  id: string;
  name: string;
  tags: string[];
  rebalance: Cadence;
  deploy: Deploy;
  funded: boolean;
  contributed: string | null;
  cash: string | null;
  positions: number;
  nav: number | null;
  target_positions: number | null;
  last_viewed: string | null;
  last_backtest: { ts: string; start: string; end: string; cost_bps: number; metrics: Metrics; benchmark: { ticker: string; metrics: Metrics } | null } | null;
  last_run: { ts: string; status: string; mode: string; source: string; error?: string; target?: string; orders: number } | null;
}

export interface StudioSettings {
  notify_on: "off" | "live" | "all";
  telegram_chat_id: string;
  weekly_digest: boolean;
  has_notify_url: boolean;
  has_telegram_token: boolean;
}

export interface OsSchedule {
  supported: boolean;
  installed: boolean;
  platform?: string;
  every_minutes?: number;
  installed_every_minutes?: number | null;
  outdated?: boolean;
  command?: string;
  detail?: string;
}

export interface Rollup {
  strategies: number;
  with_targets: number;
  total_capital: number;
  total_target: number;
  unallocated: number;
  total_held: number;
  tickers: {
    ticker: string;
    target_value: number;
    target_weight: number | null;
    held_qty: number;
    held_value: number;
    priced: boolean;
    by: { id: string; name: string; weight: number; value: number }[];
  }[];
}
