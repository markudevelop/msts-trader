import { useMemo, useState } from "react";
import { api } from "../api";
import type { BacktestResult, Metrics, Strategy } from "../types";
import { LineChart, type Series } from "./Chart";
import { NumInput, TickerInput } from "./Editor";

const pct = (v: number | null | undefined, dp = 1) => (v == null ? "–" : `${(v * 100).toFixed(dp)}%`);
const num = (v: number | null | undefined) => (v == null ? "–" : v.toFixed(2));

const ROWS: { label: string; key: keyof Metrics; fmt: (v: number | null | undefined) => string; better: "high" | "low" }[] = [
  { label: "CAGR", key: "cagr", fmt: pct, better: "high" },
  { label: "Max drawdown", key: "max_drawdown", fmt: pct, better: "low" },
  { label: "Sharpe", key: "sharpe", fmt: num, better: "high" },
  { label: "Volatility", key: "vol", fmt: pct, better: "low" },
  { label: "Total return", key: "total_return", fmt: (v) => pct(v, 0), better: "high" },
];

export function BacktestPanel({ draft }: { draft: Strategy }) {
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [cost, setCost] = useState(5);
  const [bench, setBench] = useState("SPY");
  const [log, setLog] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [res, setRes] = useState<BacktestResult | null>(null);

  const run = async () => {
    setBusy(true);
    setErr(null);
    try {
      setRes(await api<BacktestResult>("/backtest", { body: { strategy: draft, start: start || null, end: end || null, cost_bps: cost, benchmark: bench } }));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const equity = useMemo<Series[]>(() => {
    if (!res) return [];
    const s: Series[] = [{ label: draft.name, values: res.equity.map((v) => v * 10000), color: "var(--accent)" }];
    if (res.benchmark) s.push({ label: res.benchmark.ticker, values: res.benchmark.equity.map((v) => v * 10000), color: "var(--bench)", dash: [4, 3] });
    return s.map((x) => ({ ...x, color: resolve(x.color) }));
  }, [res, draft.name]);

  const drawdown = useMemo<Series[]>(() => {
    if (!res) return [];
    const dd = (eq: number[]) => {
      let peak = -Infinity;
      return eq.map((v) => {
        peak = Math.max(peak, v);
        return (v / peak - 1) * 100;
      });
    };
    const s: Series[] = [{ label: draft.name, values: dd(res.equity), color: resolve("var(--neg)"), fill: resolve("var(--neg-fill)") }];
    if (res.benchmark) s.push({ label: res.benchmark.ticker, values: dd(res.benchmark.equity), color: resolve("var(--bench)"), dash: [4, 3] });
    return s;
  }, [res, draft.name]);

  const money = useMemo(() => (v: number) => (v >= 1e6 ? `$${(v / 1e6).toFixed(2)}M` : `$${Math.round(v).toLocaleString()}`), []);
  const pctFmt = useMemo(() => (v: number) => `${v.toFixed(0)}%`, []);

  return (
    <div className="panel-stack">
      <div className="toolbar">
        <label>
          From <input type="date" value={start} onChange={(e) => setStart(e.target.value)} />
        </label>
        <label>
          To <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
        </label>
        <label>
          Cost <NumInput value={cost} onChange={setCost} min={0} max={500} /> bps
        </label>
        <label>
          vs <TickerInput value={bench} onChange={setBench} />
        </label>
        <button className="btn primary" onClick={run} disabled={busy}>
          {busy ? "Running…" : "Run backtest"}
        </button>
      </div>
      {err && <div className="alert error">{err}</div>}
      {!res && !err && !busy && (
        <div className="empty-state">
          Daily-close simulation. Weights are decided on each close and held to the next one; cost is charged on turnover.
          <br />
          Leave dates empty to use all the history your tickers have. Price data comes from Yahoo Finance and is cached locally.
        </div>
      )}
      {res && (
        <>
          <div className="metrics">
            {ROWS.map((r) => {
              const v = res.metrics[r.key] as number | null;
              const b = res.benchmark?.metrics[r.key] as number | null | undefined;
              const win = v != null && b != null && (r.better === "high" ? v > b : v < b);
              return (
                <div className="metric-card" key={r.key}>
                  <div className="metric-label">{r.label}</div>
                  <div className={`metric-value ${win ? "win" : ""}`}>{r.fmt(v)}</div>
                  {res.benchmark && (
                    <div className="metric-sub">
                      {res.benchmark.ticker} {r.fmt(b)}
                    </div>
                  )}
                </div>
              );
            })}
            <div className="metric-card">
              <div className="metric-label">Turnover / yr</div>
              <div className="metric-value">{res.metrics.annual_turnover?.toFixed(1)}×</div>
              <div className="metric-sub">{res.allocations.length} rebalances</div>
            </div>
          </div>
          <div className="card-plain">
            <div className="section-head">
              <h3>
                Growth of $10,000 <span className="muted small">{res.start} → {res.end}</span>
              </h3>
              <label className="check">
                <input type="checkbox" checked={log} onChange={(e) => setLog(e.target.checked)} /> log scale
              </label>
            </div>
            <LineChart dates={res.dates} series={equity} format={money} log={log} />
          </div>
          <div className="card-plain">
            <h3>Drawdown</h3>
            <LineChart dates={res.dates} series={drawdown} height={160} format={pctFmt} />
          </div>
          <Allocations res={res} />
        </>
      )}
    </div>
  );
}

function Allocations({ res }: { res: BacktestResult }) {
  const [all, setAll] = useState(false);
  const rows = res.allocations.slice().reverse();
  const shown = all ? rows : rows.slice(0, 12);
  return (
    <div className="card-plain">
      <h3>Allocation changes</h3>
      <table className="table">
        <thead>
          <tr>
            <th>Date</th>
            <th>Holdings</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((a) => (
            <tr key={a.date}>
              <td className="mono">{a.date}</td>
              <td>
                {Object.entries(a.weights)
                  .sort((x, y) => y[1] - x[1])
                  .map(([t, w]) => (
                    <span className="chip" key={t}>
                      {t} <b>{(w * 100).toFixed(1)}%</b>
                    </span>
                  ))}
                {Object.keys(a.weights).length === 0 && <span className="muted">cash</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > 12 && (
        <button className="btn ghost" onClick={() => setAll(!all)}>
          {all ? "Show fewer" : `Show all ${rows.length}`}
        </button>
      )}
    </div>
  );
}

function resolve(v: string) {
  const m = v.match(/^var\((--[\w-]+)\)$/);
  return m ? getComputedStyle(document.documentElement).getPropertyValue(m[1]).trim() : v;
}
