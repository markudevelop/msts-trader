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

export interface Condition {
  lhs: Metric;
  comparator: Comparator;
  rhs: Metric | null;
  rhs_value: number | null;
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

export type Node = AssetNode | GroupNode | EqualNode | SpecifiedNode | InvVolNode | IfNode | FilterNode;
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
  last_tick?: string | null;
  last_error?: string | null;
  running_now?: string | null;
  upcoming?: { strategy: string; mode: string; next_check: string; cadence: string }[];
}
