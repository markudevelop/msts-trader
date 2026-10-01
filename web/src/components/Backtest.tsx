import { useMemo, useState } from "react";
import { api } from "../api";
import type { BacktestResult, CompareResult, Metrics, Strategy } from "../types";
import { LineChart, PALETTE, cssColor, type Series } from "./Chart";
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

const BLEND_ID = "__blend__";

const money = (v: number) => (v >= 1e6 ? `$${(v / 1e6).toFixed(2)}M` : `$${Math.round(v).toLocaleString()}`);
const pctFmt = (v: number) => `${v.toFixed(0)}%`;

/** A backtest that ends >7 days ago stopped where some series' data ends (e.g. a feed's published NAV). */
function StaleEnd({ end }: { end: string }) {
  const days = (Date.now() - Date.parse(end + "T00:00:00Z")) / 86_400_000;
  if (days <= 7) return null;
  return (
    <p className="muted small">
      History ends {end}: the last day every series in this comparison has data. A pnlportfolio book's published NAV can lag its live
      weights, which live runs still use.
    </p>
  );
}

function drawdown(eq: number[]) {
  let peak = -Infinity;
  return eq.map((v) => {
    peak = Math.max(peak, v);
    return (v / peak - 1) * 100;
  });
}

export function BacktestPanel({
  draft,
  others,
  onSaved,
}: {
  draft: Strategy;
  others: { id: string; name: string }[];
  onSaved: (s: Strategy) => void;
}) {
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [cost, setCost] = useState(5);
  const [bench, setBench] = useState("SPY");
  const [log, setLog] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [res, setRes] = useState<BacktestResult | null>(null);
  const [cmp, setCmp] = useState<CompareResult | null>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const [parts, setParts] = useState<Strategy[]>([]);

  const toggle = (id: string) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id].slice(0, 7)));

  const run = async () => {
    setBusy(true);
    setErr(null);
    const common = { start: start || null, end: end || null, cost_bps: cost, benchmark: bench };
    try {
      if (picked.length) {
        const rest = await Promise.all(picked.map((id) => api<Strategy>(`/strategies/${id}`)));
        setCmp(await api<CompareResult>("/compare", { body: { strategies: [draft, ...rest], ...common } }));
        setParts([draft, ...rest]);
        setRes(null);
      } else {
        setRes(await api<BacktestResult>("/backtest", { body: { strategy: draft, ...common } }));
        setCmp(null);
      }
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

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
          {busy ? "Running…" : picked.length ? `Compare ${picked.length + 1} strategies` : "Run backtest"}
        </button>
      </div>
      {others.length > 0 && (
        <div className="compare-pick">
          <span className="muted small">Compare with</span>
          {others.map((o) => (
            <button key={o.id} className={`pick ${picked.includes(o.id) ? "on" : ""}`} onClick={() => toggle(o.id)} aria-pressed={picked.includes(o.id)}>
              {picked.includes(o.id) ? "✓ " : "+ "}
              {o.name}
            </button>
          ))}
        </div>
      )}
      {err && <div className="alert error">{err}</div>}
      {!res && !cmp && !err && !busy && (
        <div className="empty-state">
          Daily-close simulation. Weights are decided on each close and held to the next one; cost is charged on turnover.
          <br />
          Leave dates empty to use all the history your tickers have. Pick other strategies above to overlay them on the same dates.
        </div>
      )}
      {res && <Single res={res} name={draft.name} log={log} setLog={setLog} />}
      {cmp && <Compare cmp={cmp} log={log} setLog={setLog} />}
      {cmp && parts.length > 1 && (
        <CombineCard
          parts={parts}
          onResult={setCmp}
          onSaved={onSaved}
          settings={{ start: start || null, end: end || null, cost_bps: cost, benchmark: bench }}
        />
      )}
    </div>
  );
}

function Single({ res, name, log, setLog }: { res: BacktestResult; name: string; log: boolean; setLog: (v: boolean) => void }) {
  const cut = res.oos_start ? res.dates.findIndex((d) => d >= res.oos_start!) : -1;
  const equity = useMemo<Series[]>(() => {
    const eq = res.equity.map((v) => v * 10000);
    const s: Series[] =
      cut > 0
        ? [
            // Split at go-live: both halves share the cut point so the line is continuous.
            { label: `${name} · in-sample`, values: eq.map((v, i) => (i <= cut ? v : null)), color: cssColor("--accent") },
            { label: `out-of-sample (live since ${res.oos_start})`, values: eq.map((v, i) => (i >= cut ? v : null)), color: cssColor("--oos") },
          ]
        : [{ label: name, values: eq, color: cssColor("--accent") }];
    if (res.benchmark) s.push({ label: res.benchmark.ticker, values: res.benchmark.equity.map((v) => v * 10000), color: cssColor("--bench"), dash: [4, 3] });
    return s;
  }, [res, name, cut]);

  const dd = useMemo<Series[]>(() => {
    const s: Series[] = [{ label: name, values: drawdown(res.equity), color: cssColor("--neg"), fill: cssColor("--neg-fill") }];
    if (res.benchmark) s.push({ label: res.benchmark.ticker, values: drawdown(res.benchmark.equity), color: cssColor("--bench"), dash: [4, 3] });
    return s;
  }, [res, name]);

  return (
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
          <div className="metric-sub">{res.allocations.length} allocation changes</div>
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
        <StaleEnd end={res.end} />
        {cut > 0 && (
          <p className="muted small">
            Green is <b>out-of-sample</b>: the days since this strategy went live ({res.oos_start}). Everything before is in-sample, so the rules
            may have been shaped by that history. See the Deploy tab for actual live results.
          </p>
        )}
        <LineChart dates={res.dates} series={equity} format={money} log={log} />
      </div>
      <div className="card-plain">
        <h3>Drawdown</h3>
        <LineChart dates={res.dates} series={dd} height={160} format={pctFmt} />
      </div>
      <Allocations res={res} />
    </>
  );
}

function Compare({ cmp, log, setLog }: { cmp: CompareResult; log: boolean; setLog: (v: boolean) => void }) {
  const colors = useMemo(() => cmp.series.map((_, i) => cssColor(PALETTE[i % PALETTE.length])), [cmp]);
  const equity = useMemo<Series[]>(() => {
    const s: Series[] = cmp.series.map((x, i) => ({ label: x.name, values: x.equity.map((v) => v * 10000), color: colors[i] }));
    if (cmp.benchmark) s.push({ label: cmp.benchmark.ticker, values: cmp.benchmark.equity.map((v) => v * 10000), color: cssColor("--bench"), dash: [4, 3] });
    return s;
  }, [cmp, colors]);
  const dd = useMemo<Series[]>(() => cmp.series.map((x, i) => ({ label: x.name, values: drawdown(x.equity), color: colors[i] })), [cmp, colors]);

  const best = (key: keyof Metrics, better: "high" | "low") => {
    const ok = cmp.series.map((x) => x.metrics[key] as number | null).filter((v): v is number => v != null);
    if (!ok.length) return null;
    return better === "high" ? Math.max(...ok) : Math.min(...ok);
  };

  return (
    <>
      <div className="card-plain">
        <div className="section-head">
          <h3>
            Growth of $10,000 <span className="muted small">{cmp.start} → {cmp.end} · common window</span>
          </h3>
          <label className="check">
            <input type="checkbox" checked={log} onChange={(e) => setLog(e.target.checked)} /> log scale
          </label>
        </div>
        <StaleEnd end={cmp.end} />
        <LineChart dates={cmp.dates} series={equity} format={money} log={log} height={320} />
      </div>
      <div className="card-plain">
        <h3>Side by side</h3>
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th>Strategy</th>
                {ROWS.map((r) => (
                  <th key={r.key} className="num">
                    {r.label}
                  </th>
                ))}
                <th className="num">Turnover / yr</th>
              </tr>
            </thead>
            <tbody>
              {cmp.series.map((x, i) => (
                <tr key={x.id} className={x.id === BLEND_ID ? "blend-row" : ""}>
                  <td>
                    <span className="swatch" style={{ background: colors[i] }} /> {x.name}
                    {x.id === BLEND_ID && <span className="pill pill-blend">blend</span>}
                  </td>
                  {ROWS.map((r) => {
                    const v = x.metrics[r.key] as number | null;
                    const top = v != null && v === best(r.key, r.better);
                    return (
                      <td key={r.key} className={`num mono ${top ? "pos" : ""}`}>
                        {r.fmt(v)}
                      </td>
                    );
                  })}
                  <td className="num mono">{x.metrics.annual_turnover?.toFixed(1)}×</td>
                </tr>
              ))}
              {cmp.benchmark && (
                <tr className="muted">
                  <td>
                    <span className="swatch" style={{ background: cssColor("--bench") }} /> {cmp.benchmark.ticker} (buy &amp; hold)
                  </td>
                  {ROWS.map((r) => (
                    <td key={r.key} className="num mono">
                      {r.fmt(cmp.benchmark!.metrics[r.key] as number | null)}
                    </td>
                  ))}
                  <td className="num mono">–</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
      <div className="compare-grid">
        <div className="card-plain">
          <h3>Drawdown</h3>
          <LineChart dates={cmp.dates} series={dd} height={180} format={pctFmt} />
        </div>
        <div className="card-plain">
          <h3>Correlation of daily returns</h3>
          <p className="muted small">Near 1 means the strategies move together, so running both diversifies little.</p>
          <div className="table-scroll">
            <table className="table corr">
              <thead>
                <tr>
                  <th />
                  {cmp.series.map((x, i) => (
                    <th key={x.id} className="num" title={x.name}>
                      <span className="swatch" style={{ background: colors[i] }} />
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {cmp.series.map((x, i) => (
                  <tr key={x.id}>
                    <td>
                      <span className="swatch" style={{ background: colors[i] }} /> {x.name}
                    </td>
                    {cmp.correlation[i].map((c, j) => (
                      <td
                        key={j}
                        className="num mono"
                        style={{ background: i === j ? undefined : `color-mix(in srgb, var(--accent) ${Math.round(Math.abs(c) * 35)}%, transparent)` }}
                      >
                        {i === j ? "–" : c.toFixed(2)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </>
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
              <td className="mono">
                {a.date} {res.oos_start && a.date >= res.oos_start && <span className="pill pill-oos">live</span>}
              </td>
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

type Settings = { start: string | null; end: string | null; cost_bps: number; benchmark: string };

function CombineCard({
  parts,
  onResult,
  onSaved,
  settings,
}: {
  parts: Strategy[];
  onResult: (c: CompareResult) => void;
  onSaved: (s: Strategy) => void;
  settings: Settings;
}) {
  const even = Math.round((100 / parts.length) * 100) / 100;
  const [w, setW] = useState<number[]>(() => parts.map(() => even));
  const [cadence, setCadence] = useState<string>("auto");
  const [name, setName] = useState("");
  const [blend, setBlend] = useState<Strategy | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const key = parts.map((p) => p.id).join("|");
  const [lastKey, setLastKey] = useState(key);
  if (key !== lastKey) {
    // a new comparison set: reset to equal weights
    setLastKey(key);
    setW(parts.map(() => even));
    setBlend(null);
  }
  const sum = w.reduce((a, b) => a + b, 0);

  const build = () =>
    api<Strategy>("/combine", {
      body: {
        strategies: parts,
        weights: w.map((x) => x / 100),
        name: name.trim() || null,
        rebalance: cadence === "auto" ? null : cadence,
      },
    });

  const run = async () => {
    setBusy("run");
    setErr(null);
    try {
      const b = await build();
      setBlend(b);
      onResult(await api<CompareResult>("/compare", { body: { strategies: [...parts, b], ...settings } }));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const save = async () => {
    setBusy("save");
    setErr(null);
    try {
      const b = blend ?? (await build());
      const body: Partial<Strategy> = { ...b, name: name.trim() || b.name };
      delete body.id;
      onSaved(await api<Strategy>("/strategies", { body }));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="card-plain">
      <div className="section-head">
        <h3>Combine into one strategy</h3>
        <span className={`small ${Math.abs(sum - 100) < 0.01 ? "pos" : "muted"}`}>Σ {Math.round(sum * 100) / 100}%</span>
      </div>
      <p className="muted small">
        Run these strategies side by side as one book: each gets a fixed share and the blend rebalances back to those shares. Under 100% leaves
        cash; over 100% is leverage. A saved blend deploys like any other strategy.
      </p>
      <div className="combine-rows">
        {parts.map((p, i) => (
          <label key={p.id} className="combine-row">
            <span className="grow">{p.name}</span>
            <NumInput value={w[i]} onChange={(v) => setW(w.map((x, j) => (j === i ? v : x)))} min={0} max={300} step={5} />%
          </label>
        ))}
      </div>
      <div className="btn-row">
        <button className="btn ghost small" onClick={() => setW(parts.map(() => even))}>
          Equal weights
        </button>
        <label className="inline small">
          Rebalance
          <select value={cadence} onChange={(e) => setCadence(e.target.value)}>
            <option value="auto">auto (most frequent part)</option>
            {["daily", "weekly", "monthly", "quarterly", "yearly"].map((c) => (
              <option key={c}>{c}</option>
            ))}
          </select>
        </label>
        <input className="grow" placeholder="Name (optional)" value={name} onChange={(e) => setName(e.target.value)} />
      </div>
      <div className="btn-row">
        <button className="btn primary" onClick={run} disabled={!!busy || sum <= 0}>
          {busy === "run" ? "Backtesting…" : "Backtest blend"}
        </button>
        <button className="btn" onClick={save} disabled={!!busy || sum <= 0}>
          {busy === "save" ? "Saving…" : "Save as strategy"}
        </button>
      </div>
      {err && <div className="alert error small">{err}</div>}
    </div>
  );
}
