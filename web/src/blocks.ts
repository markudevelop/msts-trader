import type { Comparator, IndicatorFn, Node, Step, Strategy } from "./types";

export const INDICATOR_LABEL: Record<IndicatorFn, string> = {
  "current-price": "Current price",
  "cumulative-return": "Cumulative return %",
  "moving-average-price": "Moving avg of price",
  "exponential-moving-average-price": "EMA of price",
  "moving-average-return": "Moving avg return %",
  "relative-strength-index": "RSI",
  "standard-deviation-price": "Std dev of price",
  "standard-deviation-return": "Std dev of return %",
  "max-drawdown": "Max drawdown %",
};
export const INDICATORS = Object.keys(INDICATOR_LABEL) as IndicatorFn[];

export const COMPARATOR_LABEL: Record<Comparator, string> = { gt: ">", gte: "≥", lt: "<", lte: "≤" };

export const STEP_LABEL: Record<Step, string> = {
  asset: "Asset",
  group: "Group",
  "wt-cash-equal": "Weight · Equal",
  "wt-cash-specified": "Weight · Specified",
  "wt-inverse-vol": "Weight · Inverse volatility",
  if: "If / Else",
  filter: "Filter",
};

export const STEP_HINT: Record<Step, string> = {
  asset: "Hold one ticker",
  group: "Name a sub-strategy",
  "wt-cash-equal": "Split evenly across children",
  "wt-cash-specified": "Fixed % per child",
  "wt-inverse-vol": "Less volatile children get more",
  if: "Branch on an indicator",
  filter: "Keep the top / bottom N by an indicator",
};

export const ADDABLE: Step[] = ["asset", "wt-cash-equal", "wt-cash-specified", "wt-inverse-vol", "if", "filter", "group"];

export function newNode(step: Step): Node {
  switch (step) {
    case "asset":
      return { step, ticker: "SPY" };
    case "group":
      return { step, name: "Group", children: [] };
    case "wt-cash-equal":
    case "wt-cash-specified":
      return { step, children: [] };
    case "wt-inverse-vol":
      return { step, window: 20, children: [] };
    case "if":
      return {
        step,
        condition: {
          lhs: { fn: "relative-strength-index", ticker: "SPY", window: 10 },
          comparator: "gt",
          rhs: null,
          rhs_value: 70,
        },
        then: [],
        else: [],
      };
    case "filter":
      return { step, sort_fn: "cumulative-return", window: 20, select: "top", n: 1, children: [] };
  }
}

type Template = { key: string; name: string; blurb: string; build: () => Omit<Strategy, "id" | "deploy"> };

const asset = (ticker: string, weight?: number): Node => (weight === undefined ? { step: "asset", ticker } : { step: "asset", ticker, weight });

export const TEMPLATES: Template[] = [
  {
    key: "blank",
    name: "Blank",
    blurb: "An equal-weight block to start from",
    build: () => ({ name: "New strategy", description: "", rebalance: "daily", children: [{ step: "wt-cash-equal", children: [asset("SPY")] }] }),
  },
  {
    key: "6040",
    name: "Classic 60/40",
    blurb: "SPY / AGG, rebalanced monthly",
    build: () => ({
      name: "Classic 60/40",
      description: "60% US stocks, 40% bonds.",
      rebalance: "monthly",
      children: [{ step: "wt-cash-specified", children: [asset("SPY", 0.6), asset("AGG", 0.4)] }],
    }),
  },
  {
    key: "trend",
    name: "200-day trend",
    blurb: "SPY above its 200-day average, else T-bills",
    build: () => ({
      name: "SPY 200-day trend",
      description: "Hold SPY while it trades above its 200-day moving average; otherwise sit in BIL.",
      rebalance: "daily",
      children: [
        {
          step: "if",
          condition: {
            lhs: { fn: "current-price", ticker: "SPY", window: 1 },
            comparator: "gt",
            rhs: { fn: "moving-average-price", ticker: "SPY", window: 200 },
            rhs_value: null,
          },
          then: [asset("SPY")],
          else: [asset("BIL")],
        },
      ],
    }),
  },
  {
    key: "dual",
    name: "Dual momentum",
    blurb: "Best of US / intl stocks, bonds if both weak",
    build: () => ({
      name: "Dual momentum",
      description: "Pick the stronger of SPY and EFA over 12 months; fall back to AGG when SPY's 12-month return trails T-bills.",
      rebalance: "monthly",
      children: [
        {
          step: "if",
          condition: {
            lhs: { fn: "cumulative-return", ticker: "SPY", window: 252 },
            comparator: "gt",
            rhs: { fn: "cumulative-return", ticker: "BIL", window: 252 },
            rhs_value: null,
          },
          then: [{ step: "filter", sort_fn: "cumulative-return", window: 252, select: "top", n: 1, children: [asset("SPY"), asset("EFA")] }],
          else: [asset("AGG")],
        },
      ],
    }),
  },
  {
    key: "rsi",
    name: "RSI guard",
    blurb: "QQQ, stepping into bonds and gold when overbought",
    build: () => ({
      name: "QQQ RSI guard",
      description: "Hold QQQ; when its 10-day RSI is stretched above 79, rotate into an inverse-volatility mix of bonds and gold.",
      rebalance: "daily",
      children: [
        {
          step: "if",
          condition: { lhs: { fn: "relative-strength-index", ticker: "QQQ", window: 10 }, comparator: "gt", rhs: null, rhs_value: 79 },
          then: [{ step: "wt-inverse-vol", window: 20, children: [asset("TLT"), asset("GLD")] }],
          else: [asset("QQQ")],
        },
      ],
    }),
  },
];

/** Every ticker referenced anywhere in the tree. */
export function treeTickers(nodes: Node[]): string[] {
  const out = new Set<string>();
  const walk = (ns: Node[]) =>
    ns.forEach((n) => {
      if (n.step === "asset") out.add(n.ticker);
      else if (n.step === "if") {
        out.add(n.condition.lhs.ticker);
        if (n.condition.rhs) out.add(n.condition.rhs.ticker);
        walk(n.then);
        walk(n.else);
      } else walk(n.children);
    });
  walk(nodes);
  return [...out].filter(Boolean).sort();
}
