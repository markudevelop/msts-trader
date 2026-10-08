import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { CashModal } from "./Home";
import type { Deploy, Meta, OosResult, OrderType, OsSchedule, RunEntry, SchedulerState, SleeveLedger, Strategy } from "../types";
import { LineChart, cssColor, type Series } from "./Chart";
import { NumInput } from "./Editor";

type Props = {
  draft: Strategy;
  saved: Strategy;
  dirty: boolean;
  meta: Meta | null;
  onDeploy: (d: Deploy) => void;
  onSave: () => Promise<void>;
};

const money = (s: string | number | null | undefined) =>
  s == null ? "–" : `$${Number(s).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

export function DeployPanel({ draft, saved, dirty, meta, onDeploy, onSave }: Props) {
  const d = draft.deploy;
  const set = (patch: Partial<Deploy>) => onDeploy({ ...d, ...patch });
  const [runs, setRuns] = useState<RunEntry[]>([]);
  const [sleeve, setSleeve] = useState<SleeveLedger[] | null>(null);
  const [sched, setSched] = useState<SchedulerState | null>(null);
  const [last, setLast] = useState<RunEntry | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [cashOpen, setCashOpen] = useState(false);
  const [tick, setTick] = useState(0);

  const refresh = useCallback(async () => {
    const [r, s, sc] = await Promise.all([
      api<RunEntry[]>(`/runs?strategy=${encodeURIComponent(saved.id)}&limit=20`),
      api<{ ledgers: SleeveLedger[] }>(`/strategies/${saved.id}/sleeve`),
      api<SchedulerState>("/scheduler"),
    ]);
    setRuns(r);
    setSleeve(s.ledgers);
    setSched(sc);
    setTick((t) => t + 1);
  }, [saved.id]);

  useEffect(() => {
    setLast(null);
    refresh().catch((e) => setErr(e.message));
  }, [refresh, saved.deploy.broker]);

  const act = async (label: string, fn: () => Promise<void>) => {
    setBusy(label);
    setErr(null);
    try {
      await fn();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
      refresh().catch(() => undefined);
    }
  };

  const preview = () =>
    act("preview", async () => {
      setLast(await api<RunEntry>(`/strategies/${saved.id}/run`, { body: { mode: "dry" } }));
    });

  const executeLive = (confirm: string) =>
    act("live", async () => {
      setConfirming(false);
      setLast(await api<RunEntry>(`/strategies/${saved.id}/run`, { body: { mode: "live", confirm } }));
    });

  const isPaper = saved.deploy.broker === "paper";
  const liveReady = saved.deploy.live_enabled && !dirty;
  const mocBrokers = meta?.moc_brokers ?? ["alpaca", "ibkr", "schwab", "paper"];
  const mocOk = mocBrokers.includes(d.broker);
  const orderType: OrderType = d.order_type ?? "market";
  // Latest scheduled start for MOC on a regular 16:00 ET close.
  const mocLatest = useMemo(() => {
    const m = 16 * 60 - (meta?.moc_lead_minutes ?? 15);
    return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
  }, [meta?.moc_lead_minutes]);

  return (
    <div className="deploy-grid">
      <div className="panel-stack">
        <section className="card-plain">
          <h3>Broker &amp; schedule</h3>
          <div className="form-grid">
            <label>
              Broker
              <select
                value={d.broker}
                onChange={(e) => {
                  const broker = e.target.value;
                  // A broker without MOC can't keep a MOC order type.
                  set({ broker, live_enabled: false, ...(mocBrokers.includes(broker) ? {} : { order_type: "market" }) });
                }}
              >
                {(meta?.brokers ?? ["paper"]).map((b) => (
                  <option key={b} value={b}>
                    {b}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Account <span className="muted small">(optional: full number or last 4)</span>
              <input value={d.account ?? ""} placeholder="default account" onChange={(e) => set({ account: e.target.value.trim() || null })} />
            </label>
            <label>
              Drift threshold
              <span className="inline">
                <NumInput value={Math.round(d.threshold * 10000) / 100} onChange={(v) => set({ threshold: v / 100 })} min={0} max={100} step={0.5} />% of each position
              </span>
            </label>
            <label>
              Order type
              <select value={orderType} onChange={(e) => set({ order_type: e.target.value as OrderType })}>
                <option value="market">Market (fills when the run executes)</option>
                <option value="moc" disabled={!mocOk}>
                  Market-on-close (fills in the closing auction){mocOk ? "" : ` (not supported on ${d.broker})`}
                </option>
              </select>
              <span className="muted small">
                {orderType === "moc"
                  ? `Exchanges stop accepting MOC orders around 15:50 ET, so scheduled runs start no later than ${mocLatest} ET (earlier on half-days). Whole shares only.`
                  : "Recommended for scheduled runs near the close."}
              </span>
            </label>
            <label className="check">
              <input type="checkbox" checked={d.schedule_enabled} onChange={(e) => set({ schedule_enabled: e.target.checked })} />
              Run automatically at
              <input type="time" value={d.schedule_time} disabled={!d.schedule_enabled} onChange={(e) => set({ schedule_time: e.target.value })} /> New
              York time (ET), {draft.rebalance}
              <span className="muted small">
                {" "}
                = {etToLocal(d.schedule_time)} your time
              </span>
            </label>
            {d.schedule_enabled && orderType === "moc" && d.schedule_time > mocLatest && (
              <div className="alert warn small">
                Market-on-close: this strategy will run at {mocLatest} ET instead of {d.schedule_time}, the latest time MOC orders are still
                accepted with margin. Pick Market to keep {d.schedule_time}.
              </div>
            )}
          </div>
          <div className={`live-toggle ${d.live_enabled ? "on" : ""}`}>
            <label className="check">
              <input type="checkbox" checked={d.live_enabled} onChange={(e) => set({ live_enabled: e.target.checked })} />
              <b>Allow live orders on {d.broker}</b>
            </label>
            <p className="muted small">
              {d.broker === "paper"
                ? "Paper trading is simulated locally; no real orders. Home lists it under Incubation."
                : d.paper_account
                  ? "Paper / sandbox account: orders go to the broker's test environment, no real money. Home lists it under Incubation."
                  : "Real money. Scheduled runs place orders without asking. Manual runs still need a typed confirmation."}
            </p>
            {d.broker !== "paper" && (
              <label className="check small">
                <input type="checkbox" checked={!!d.paper_account} onChange={(e) => set({ paper_account: e.target.checked })} />
                This is a paper / sandbox account (e.g. Alpaca paper, Tradier sandbox): list it under Incubation, not Live
              </label>
            )}
          </div>
          {dirty && (
            <div className="alert warn">
              Unsaved changes. Runs use the saved version.{" "}
              <button className="btn small" onClick={() => onSave()}>
                Save now
              </button>
            </div>
          )}
        </section>

        <section className="card-plain">
          <div className="section-head">
            <h3>Run</h3>
            <span className="muted small">
              market {meta?.market.status ?? "…"}
              {meta?.market.minutes_to_close != null ? ` · closes in ${meta.market.minutes_to_close} min` : ""}
            </span>
          </div>
          <p className="muted small">
            Evaluates the strategy on the latest prices, then previews the orders its sleeve <code>{saved.id}</code> needs on{" "}
            <b>{saved.deploy.broker}</b>. Runs through the same engine as <code>msts-trader rebalance --sleeve</code>.
          </p>
          <div className="btn-row">
            <button className="btn" onClick={preview} disabled={!!busy || dirty}>
              {busy === "preview" ? "Evaluating…" : "Preview orders"}
            </button>
            <button
              className={`btn ${isPaper ? "primary" : "danger"}`}
              onClick={() => setConfirming(true)}
              disabled={!!busy || !liveReady}
              title={!saved.deploy.live_enabled ? "Enable live orders above first" : dirty ? "Save first" : ""}
            >
              {busy === "live" ? "Executing…" : isPaper ? "Execute on paper" : `Execute LIVE on ${saved.deploy.broker}`}
            </button>
            <span className="spacer" />
            <button
              className="btn ghost danger-text"
              onClick={() => setCashOpen(true)}
              disabled={!!busy || !sleeve?.some((l) => Object.keys(l.holdings).length)}
              title="Sell everything this strategy holds and pause its schedule"
            >
              Go to cash…
            </button>
          </div>
          {err && <div className="alert error">{err}</div>}
          {last && <RunResult run={last} />}
        </section>

        <LivePerformance sid={saved.id} tick={tick} />

        <section className="card-plain">
          <h3>History</h3>
          {runs.length === 0 ? (
            <p className="muted small">No runs yet.</p>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>When (UTC)</th>
                  <th>Mode</th>
                  <th>Source</th>
                  <th>Status</th>
                  <th>Detail</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r, i) => (
                  <tr key={i}>
                    <td className="mono">{r.ts.replace("T", " ").replace("Z", "")}</td>
                    <td>
                      <span className={`pill ${r.mode === "live" ? "pill-live" : ""}`}>{r.mode}</span>
                    </td>
                    <td>{r.source}</td>
                    <td>
                      <span className={`status status-${r.status}`}>{r.status}</span>
                    </td>
                    <td className="small">
                      {r.error ??
                        (r.execution
                          ? `sent ${r.execution.sent}, failed ${r.execution.failed}`
                          : r.preview
                            ? `${r.preview.orders.length} order(s)`
                            : "")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </div>

      <div className="panel-stack">
        <Capital sid={saved.id} ledgers={sleeve} disabled={dirty} onChange={setSleeve} />
        <section className="card-plain">
          <h3>Automation</h3>
          {sched?.running ? (
            <p className="small">
              Scheduler running{sched.last_tick ? ` · last check ${sched.last_tick.slice(11, 19)} ET` : ""}.
              {sched.running_now && <b> Running {sched.running_now}…</b>}
            </p>
          ) : (
            <p className="muted small">Scheduler is off (started with --no-scheduler).</p>
          )}
          {sched?.last_error && <div className="alert error small">{sched.last_error}</div>}
          {sched?.market_now && (
            <p className="muted small">
              Runs on the New York market clock, not this computer's. Now: <b>{sched.market_now.slice(11, 16)} ET</b> · {sched.local_now?.slice(11, 16)}{" "}
              {sched.local_tz} here.
            </p>
          )}
          {sched?.upcoming?.filter((u) => u.strategy === saved.id).map((u) => (
            <p className="small" key={u.strategy}>
              Next check <b>{u.next_check.replace("T", " ").slice(0, 16)} ET</b> ({localTime(u.next_check)} your time) ·{" "}
              {u.mode === "live" ? "LIVE" : "dry-run"} · {u.cadence}
            </p>
          ))}
          <OsTask />
        </section>
      </div>
      {confirming && <ConfirmLive strategy={saved} onCancel={() => setConfirming(false)} onConfirm={executeLive} />}
      {cashOpen && (
        <CashModal
          rows={[{ id: saved.id, name: saved.name, deploy: saved.deploy, tags: [], rebalance: saved.rebalance, funded: true, stage: "incubation", contributed: null, cash: null, nav: null, positions: 0, target_positions: null, last_viewed: null, last_backtest: null, last_run: null }]}
          onClose={() => setCashOpen(false)}
          onDone={async () => {
            setCashOpen(false);
            await refresh();
          }}
        />
      )}
    </div>
  );
}

function RunResult({ run }: { run: RunEntry }) {
  const p = run.preview;
  return (
    <div className="run-result">
      <div className="section-head">
        <span className={`status status-${run.status}`}>{run.status}</span>
        <span className="muted small">
          signal as of {run.asof ?? "–"}
          {p ? ` · account NAV ${money(p.nav)}` : ""}
        </span>
      </div>
      {run.error && <div className="alert error">{run.error}</div>}
      {run.weights && (
        <div className="chips">
          {Object.entries(run.weights).map(([t, w]) => (
            <span className="chip" key={t}>
              {t} <b>{(w * 100).toFixed(1)}%</b>
            </span>
          ))}
        </div>
      )}
      {p?.warnings?.map((w, i) => (
        <div className="alert warn small" key={i}>
          {w}
        </div>
      ))}
      {p && p.orders.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>Ticker</th>
              <th>Side</th>
              <th className="num">Qty</th>
              <th className="num">~Price</th>
              <th className="num">Notional</th>
            </tr>
          </thead>
          <tbody>
            {p.orders.map((o, i) => (
              <tr key={i}>
                <td className="mono">{o.ticker}</td>
                <td className={o.side === "BUY" ? "pos" : "neg"}>{o.side}</td>
                <td className="num mono">{o.quantity}</td>
                <td className="num mono">{o.estimated_price ? money(o.estimated_price) : "–"}</td>
                <td className="num mono">{money(o.notional)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {run.execution && (
        <p className="small">
          Sent {run.execution.sent}, failed {run.execution.failed}
          {run.execution.verify ? ` · post-trade verify: ${run.execution.verify.converged ? "on target" : "residual drift"}` : ""}
        </p>
      )}
    </div>
  );
}

function Capital({
  sid,
  ledgers,
  disabled,
  onChange,
}: {
  sid: string;
  ledgers: SleeveLedger[] | null;
  disabled: boolean;
  onChange: (l: SleeveLedger[]) => void;
}) {
  const [amount, setAmount] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const go = async (action: "invest" | "divest") => {
    setBusy(true);
    setErr(null);
    try {
      const r = await api<{ sleeve: SleeveLedger[] }>(`/strategies/${sid}/capital`, { body: { action, amount } });
      onChange(r.sleeve);
      setAmount("");
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const valid = Number(amount) > 0;
  return (
    <section className="card-plain">
      <h3>Capital</h3>
      <p className="muted small">
        The strategy trades as its own <b>sleeve</b> inside your account. It sizes against its own capital, compounds its own P&amp;L, and never
        touches your other positions. Investing only earmarks money; nothing is transferred.
      </p>
      {ledgers === null ? (
        <p className="muted small">Loading…</p>
      ) : ledgers.length === 0 ? (
        <p className="small">No capital yet. Invest to size the strategy (otherwise it sizes against the full account NAV).</p>
      ) : (
        ledgers.map((l) => (
          <div className="sleeve" key={l.account}>
            <div className="kv">
              <span>Account</span>
              <b className="mono">{l.account}</b>
            </div>
            <div className="kv">
              <span>Uninvested cash</span>
              <b>{money(l.cash)}</b>
            </div>
            <div className="kv">
              <span>Contributed</span>
              <b>{money(l.contributed)}</b>
            </div>
            <div className="chips">
              {Object.entries(l.holdings).map(([t, q]) => (
                <span className="chip" key={t}>
                  {t} <b>{Number(q).toLocaleString()}</b> sh
                </span>
              ))}
            </div>
            {l.pending.length > 0 && <div className="alert warn small">{l.pending.length} order(s) still working at the broker</div>}
          </div>
        ))
      )}
      <div className="btn-row">
        <input className="amount" placeholder="Amount $" inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value.replace(/[^0-9.]/g, ""))} />
        <button className="btn" disabled={!valid || busy || disabled} onClick={() => go("invest")}>
          Invest
        </button>
        <button className="btn ghost" disabled={!valid || busy || disabled} onClick={() => go("divest")}>
          Withdraw
        </button>
      </div>
      {err && <div className="alert error small">{err}</div>}
    </section>
  );
}

function ConfirmLive({ strategy, onCancel, onConfirm }: { strategy: Strategy; onCancel: () => void; onConfirm: (c: string) => void }) {
  const [text, setText] = useState("");
  const paper = strategy.deploy.broker === "paper";
  return (
    <div className="modal-backdrop" onMouseDown={onCancel}>
      <div className="modal" role="dialog" aria-modal="true" onMouseDown={(e) => e.stopPropagation()}>
        <h3>{paper ? "Execute on paper" : "Place real orders"}</h3>
        <p>
          This evaluates <b>{strategy.name}</b> now and sends the resulting orders to <b>{strategy.deploy.broker}</b>
          {strategy.deploy.account ? ` (account ${strategy.deploy.account})` : ""}. Preview first if you haven't.
        </p>
        <p className="small">
          Type <code>{strategy.id}</code> to confirm.
        </p>
        <input autoFocus value={text} onChange={(e) => setText(e.target.value)} onKeyDown={(e) => e.key === "Enter" && text === strategy.id && onConfirm(text)} />
        <div className="btn-row end">
          <button className="btn ghost" onClick={onCancel}>
            Cancel
          </button>
          <button className={`btn ${paper ? "primary" : "danger"}`} disabled={text !== strategy.id} onClick={() => onConfirm(text)}>
            Execute
          </button>
        </div>
      </div>
    </div>
  );
}

const pctS = (v: number | null | undefined, dp = 2) => (v == null ? "–" : `${v >= 0 ? "+" : ""}${(v * 100).toFixed(dp)}%`);

function LivePerformance({ sid, tick }: { sid: string; tick: number }) {
  const [perf, setPerf] = useState<OosResult | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    setErr(null);
    api<OosResult>(`/strategies/${sid}/performance`)
      .then((p) => alive && setPerf(p))
      .catch((e) => alive && setErr(e.message));
    return () => {
      alive = false;
    };
  }, [sid, tick]);

  const growth = useMemo<Series[]>(() => {
    if (!perf?.dates || !perf.actual) return [];
    const s: Series[] = [{ label: "Actual (your sleeve)", values: perf.actual.index.map((v) => (v - 1) * 100), color: cssColor("--oos") }];
    if (perf.model) s.push({ label: "Model (backtest)", values: perf.model.index.map((v) => (v == null ? null : (v - 1) * 100)), color: cssColor("--accent"), dash: [5, 3] });
    if (perf.benchmark) s.push({ label: perf.benchmark.ticker, values: perf.benchmark.index.map((v) => (v - 1) * 100), color: cssColor("--bench"), dash: [2, 3] });
    return s;
  }, [perf]);
  const pnl = useMemo<Series[]>(() => {
    if (!perf?.dates || !perf.actual) return [];
    const { nav, contributed } = perf.actual;
    return [{ label: "P&L $", values: nav.map((v, i) => (contributed[i] == null ? null : v - (contributed[i] as number))), color: cssColor("--oos"), fill: "rgba(71,205,137,0.10)" }];
  }, [perf]);
  const pctFmt = useMemo(() => (v: number) => `${v.toFixed(1)}%`, []);
  const usd = useMemo(() => (v: number) => `${v < 0 ? "-" : ""}$${Math.abs(Math.round(v)).toLocaleString()}`, []);

  const m = perf?.metrics;
  return (
    <section className="card-plain">
      <div className="section-head">
        <h3>Live performance (out-of-sample)</h3>
        {perf?.live_since && <span className="muted small">since go-live {perf.live_since}</span>}
      </div>
      {err && <div className="alert error small">{err}</div>}
      {perf && !perf.live_since && (
        <p className="muted small">
          Tracking starts with the first executed live run (paper counts). From then on, the sleeve's real value is marked at every close and
          compared with what the backtest says the strategy should have done.
        </p>
      )}
      {perf?.live_since && m && (
        <>
          <div className="metrics compact">
            <div className="metric-card">
              <div className="metric-label">Actual return</div>
              <div className={`metric-value ${(m.actual_return ?? 0) >= 0 ? "win" : "lose"}`}>{pctS(m.actual_return)}</div>
              <div className="metric-sub">time-weighted</div>
            </div>
            <div className="metric-card">
              <div className="metric-label">P&amp;L</div>
              <div className={`metric-value ${(m.pnl ?? 0) >= 0 ? "win" : "lose"}`}>{m.pnl == null ? "–" : usd(m.pnl)}</div>
              <div className="metric-sub">NAV {m.nav == null ? "–" : usd(m.nav)}</div>
            </div>
            <div className="metric-card">
              <div className="metric-label">Model</div>
              <div className="metric-value">{pctS(m.model_return)}</div>
              <div className="metric-sub" title="Actual minus model: execution cost, timing, rounding">gap {pctS(m.tracking_gap)}</div>
            </div>
            {perf.benchmark && (
              <div className="metric-card">
                <div className="metric-label">{perf.benchmark.ticker}</div>
                <div className="metric-value">{pctS(m.benchmark_return)}</div>
                <div className="metric-sub">max DD (actual) {pctS(m.actual_max_drawdown == null ? null : -m.actual_max_drawdown)}</div>
              </div>
            )}
          </div>
          {perf.dates && perf.dates.length > 1 ? (
            <>
              <LineChart dates={perf.dates} series={growth} height={220} format={pctFmt} />
              <LineChart dates={perf.dates} series={pnl} height={140} format={usd} />
            </>
          ) : (
            <p className="muted small">
              The first point is the close on {perf.live_since} (4:00 pm ET); the chart fills in from there.
            </p>
          )}
          {perf.model_error && <p className="muted small">Model unavailable: {perf.model_error}</p>}
        </>
      )}
    </section>
  );
}

/** An ISO timestamp with offset, shown in the browser's own timezone. */
function localTime(iso: string) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? "–"
    : d.toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit", timeZoneName: "short" });
}

/** "HH:MM" New York time -> the same instant on the viewer's clock (today's DST offsets). */
function etToLocal(hhmm: string) {
  const [h, m] = hhmm.split(":").map(Number);
  if (!Number.isFinite(h) || !Number.isFinite(m)) return "–";
  const now = new Date();
  const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), h, m));
  // Find the UTC instant whose New York wall clock reads hh:mm (DST-safe: ask Intl for NY's offset that day).
  const nyParts = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(d);
  const nyH = Number(nyParts.find((p) => p.type === "hour")?.value);
  const nyM = Number(nyParts.find((p) => p.type === "minute")?.value);
  let diff = (h * 60 + m - (nyH * 60 + nyM)) % 1440;
  if (diff > 720) diff -= 1440;
  if (diff < -720) diff += 1440;
  const at = new Date(d.getTime() + diff * 60_000);
  return at.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", timeZoneName: "short" });
}

const every = (m: number | null | undefined) => (m == null ? "periodically" : m === 1 ? "every minute" : `every ${m} minutes`);

/** Run schedules with Studio closed: one OS task calling `strategy run-due`. */
function OsTask() {
  const [st, setSt] = useState<OsSchedule | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    api<OsSchedule>("/os-schedule").then(setSt).catch((e) => setErr(e.message));
  }, []);
  const set = async (install: boolean) => {
    setBusy(true);
    setErr(null);
    try {
      setSt(await api<OsSchedule>("/os-schedule", { body: { install } }));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const where = st?.platform === "windows" ? "Windows Task Scheduler" : "cron";
  return (
    <div className="os-task">
      <h4>Run without Studio open</h4>
      {!st ? (
        <p className="muted small">{err ?? "Checking…"}</p>
      ) : !st.supported ? (
        <p className="muted small">No task scheduler found on this system. Use cron, Task Scheduler or GitHub Actions with <code>msts-trader strategy run-due</code>.</p>
      ) : st.installed ? (
        <>
          <p className="small pos">
            ✓ Installed in {where}: checks {every(st.installed_every_minutes ?? st.every_minutes)} and runs whatever is due, even with Studio closed
            {st.platform === "windows" ? " (while you're logged in)" : ""}.
          </p>
          {st.outdated && (
            <div className="alert warn small">
              This task was installed by an older version and checks {every(st.installed_every_minutes)}: a run can start up to that late, and a schedule
              in the last minutes before the close can be missed. Update it to check {every(st.every_minutes)}.
            </div>
          )}
          <div className="btn-row">
            {st.outdated && (
              <button className="btn small primary" disabled={busy} onClick={() => set(true)}>
                {busy ? "Updating…" : `Update to ${every(st.every_minutes)}`}
              </button>
            )}
            <button className="btn small ghost" disabled={busy} onClick={() => set(false)}>
              {busy ? "Removing…" : "Remove task"}
            </button>
          </div>
        </>
      ) : (
        <>
          <p className="muted small">
            Right now schedules only run while Studio is open. Install one {where} task that checks {every(st.every_minutes)} and runs every strategy
            that's due, with the same rules and the same once-per-period guard, so it never double-trades with an open Studio.
          </p>
          <button className="btn small" disabled={busy} onClick={() => set(true)}>
            {busy ? "Installing…" : `Install ${where} task`}
          </button>
        </>
      )}
      {err && st && <div className="alert error small">{err}</div>}
    </div>
  );
}
