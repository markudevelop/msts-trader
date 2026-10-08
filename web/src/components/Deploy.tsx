import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { CashModal } from "./Home";
import type { Deploy, Meta, OosResult, OrderType, OsSchedule, RunEntry, SchedulerState, SleeveAmount, SleeveLedger, Strategy } from "../types";
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
  const extBrokers = meta?.extended_brokers ?? ["tastytrade", "alpaca", "tradier", "ibkr", "schwab", "paper"];
  const extOk = extBrokers.includes(d.broker);
  const orderType: OrderType = d.order_type ?? "market";
  const chasing = orderType === "limit-chase" || orderType === "extended";
  // Latest scheduled start: before a regular 16:00 ET close for MOC / limit
  // chase, before the 20:00 ET end of after-hours for extended hours.
  const lead =
    orderType === "moc"
      ? (meta?.moc_lead_minutes ?? 15)
      : orderType === "limit-chase"
        ? (meta?.chase_lead_minutes ?? 15)
        : orderType === "extended"
          ? (meta?.extended_lead_minutes ?? 10)
          : null;
  const latest = useMemo(() => {
    const m = (orderType === "extended" ? 20 : 16) * 60 - (lead ?? 10);
    return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
  }, [lead, orderType]);

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
                  // A broker without MOC / extended hours can't keep that order type.
                  const keep =
                    (orderType !== "moc" || mocBrokers.includes(broker)) && (orderType !== "extended" || extBrokers.includes(broker));
                  set({ broker, live_enabled: false, ...(keep ? {} : { order_type: "market" }) });
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
                <option value="limit-chase">Limit chase (limit near the mid, then market)</option>
                <option value="extended" disabled={!extOk}>
                  Extended hours (limit only, premarket and after-hours){extOk ? "" : ` (not supported on ${d.broker})`}
                </option>
              </select>
              <span className="muted small">
                {orderType === "moc"
                  ? `Exchanges stop accepting MOC orders around 15:50 ET, so scheduled runs start no later than ${latest} ET (earlier on half-days). Whole shares only.`
                  : orderType === "limit-chase"
                    ? `Each order is a limit at the mid, repriced a few times, then a market order for anything unfilled. Pays less spread but takes about 30 s per order, so scheduled runs start no later than ${latest} ET (earlier on half-days).`
                    : orderType === "extended"
                      ? `Runs any time from 04:00 to ${latest} ET on trading days, including premarket and after-hours. Limit orders only: anything still unfilled after the last reprice is cancelled, not sent at market. Your broker's own extended session and permissions apply.`
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
            {d.schedule_enabled && lead !== null && d.schedule_time > latest && (
              <div className="alert warn small">
                {orderType === "moc"
                  ? `Market-on-close: this strategy will run at ${latest} ET instead of ${d.schedule_time}, the latest time MOC orders are still accepted with margin. Pick Market to keep ${d.schedule_time}.`
                  : orderType === "limit-chase"
                    ? `Limit chase: this strategy will run at ${latest} ET instead of ${d.schedule_time}, so every order can finish before the close. Pick Market to keep ${d.schedule_time}.`
                    : `Extended hours: this strategy will run at ${latest} ET instead of ${d.schedule_time}, before after-hours trading ends at 20:00 ET.`}
              </div>
            )}
            {d.schedule_enabled && orderType === "extended" && d.schedule_time < "04:00" && (
              <div className="alert warn small">Extended hours: this strategy will run at 04:00 ET, when premarket trading opens.</div>
            )}
            {d.schedule_enabled && orderType !== "extended" && (d.schedule_time < "09:30" || d.schedule_time >= "16:00") && (
              <div className="alert warn small">
                {d.schedule_time} ET is outside regular trading hours (09:30–16:00), so this strategy's orders would be refused. Pick Extended hours to trade
                premarket or after-hours.
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
          <h3>Execution</h3>
          <div className="form-grid">
            <label>
              Rebalance scope
              <select value={d.rebalance_scope ?? "whole-book"} onChange={(e) => set({ rebalance_scope: e.target.value as Deploy["rebalance_scope"] })}>
                <option value="whole-book">Whole book (any drift snaps every position to target)</option>
                <option value="per-ticker">Per ticker (trade only the positions that drifted)</option>
              </select>
              <span className="muted small">Per ticker trades less often; whole book tracks the strategy more closely.</span>
            </label>
            <label>
              Minimum weight
              <span className="inline">
                <NumInput value={Math.round((d.min_weight ?? 0) * 10000) / 100} onChange={(v) => set({ min_weight: v / 100 })} min={0} max={100} step={0.5} />% —
                skip smaller targets (0 = trade all)
              </span>
            </label>
            <label>
              Max buys per run <span className="muted small">(optional safety cap)</span>
              <span className="inline">
                $<OptNum value={d.max_notional ?? null} onChange={(v) => set({ max_notional: v })} min={1} step={1000} placeholder="no cap" wide />
              </span>
            </label>
            <label className="check">
              <input
                type="checkbox"
                checked={orderType === "moc" || !!d.whole_shares}
                disabled={orderType === "moc"}
                onChange={(e) => set({ whole_shares: e.target.checked })}
              />
              Whole shares only <span className="muted small">{orderType === "moc" ? "(always for market-on-close)" : "(round every order down)"}</span>
            </label>
            {chasing && (
              <>
                <label>
                  Chase reprices <span className="muted small">(blank = default 5)</span>
                  <OptNum value={d.chase_retries ?? null} onChange={(v) => set({ chase_retries: v === null ? null : Math.round(v) })} min={1} max={50} placeholder="5" />
                </label>
                <label>
                  Seconds per reprice <span className="muted small">(blank = default 5)</span>
                  <OptNum value={d.chase_interval ?? null} onChange={(v) => set({ chase_interval: v })} min={0.5} max={120} step={0.5} placeholder="5" />
                </label>
                <label>
                  Aggression <span className="muted small">(% past the mid toward a fill; blank = 0)</span>
                  <span className="inline">
                    <OptNum
                      value={d.chase_aggression == null ? null : Math.round(d.chase_aggression * 100000) / 1000}
                      onChange={(v) => set({ chase_aggression: v === null ? null : v / 100 })}
                      min={0}
                      max={5}
                      step={0.05}
                      placeholder="0"
                    />
                    %
                  </span>
                </label>
              </>
            )}
          </div>
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
        <SleeveTools sid={saved.id} ledgers={sleeve} disabled={dirty} onChange={setSleeve} />
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

const describeAmount = (a: SleeveAmount | undefined, none: string) =>
  !a ? none : a.mode === "pct-nav" ? `${+(Number(a.value) * 100).toFixed(4)}% of account NAV` : money(a.value);

type ShareAction = "adopt" | "release" | "adjust";
const SHARE_HELP: Record<ShareAction, string> = {
  adopt: "Give shares you already hold (and no strategy owns) to this strategy. Nothing is traded.",
  release: "Hand shares back to your manual book. The strategy stops managing them. Nothing is traded.",
  adjust: "Overwrite the strategy's share count for one ticker, e.g. after a corporate action or a manual sale. Nothing is traded.",
};

/** The `msts-trader sleeve` admin commands for this strategy's sleeve. */
function SleeveTools({
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
  const [shareAction, setShareAction] = useState<ShareAction>("adopt");
  const [ticker, setTicker] = useState("");
  const [qty, setQty] = useState("");
  const [baseMode, setBaseMode] = useState<"own-nav" | "pct" | "usd">("own-nav");
  const [baseValue, setBaseValue] = useState("");
  const [capMode, setCapMode] = useState<"off" | "pct" | "usd">("off");
  const [capValue, setCapValue] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [result, setResult] = useState<{ ok: boolean; output: string } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const policy = ledgers?.[0]?.policy ?? {};

  const run = async (label: string, body: Record<string, string>) => {
    setBusy(label);
    setErr(null);
    setResult(null);
    try {
      const r = await api<{ ok: boolean; output: string; sleeve: SleeveLedger[] }>(`/strategies/${sid}/sleeve-tool`, { body });
      onChange(r.sleeve);
      setResult({ ok: r.ok, output: r.output });
      return r.ok;
    } catch (e) {
      setErr((e as Error).message);
      return false;
    } finally {
      setBusy(null);
    }
  };
  const spec = (mode: string, value: string) => (mode === "pct" ? `${value}%` : mode === "usd" ? `$${value}` : mode);
  const shareValid = /^[A-Za-z0-9][A-Za-z0-9.\-^=/]{0,19}$/.test(ticker.trim()) && qty !== "" && (Number(qty) > 0 || (shareAction === "adjust" && Number(qty) === 0));
  const amountOk = (mode: string, value: string) => mode === "own-nav" || mode === "off" || Number(value) > 0;

  return (
    <section className="card-plain">
      <h3>Sleeve tools</h3>
      <p className="muted small">Bookkeeping for this strategy's sleeve. None of these place orders.</p>

      <h4 className="small">Reconcile</h4>
      <p className="muted small">
        Settle orders still pending, then compare every sleeve's shares with what the account really holds. A negative "Unassigned" means a sleeve
        claims shares the account no longer has; fix it with Set tally or Release before the next run.
      </p>
      <div className="btn-row">
        <button className="btn ghost" disabled={!!busy || disabled} onClick={() => run("reconcile", { action: "reconcile" })}>
          {busy === "reconcile" ? "Reconciling…" : "Reconcile account"}
        </button>
      </div>

      <h4 className="small">Shares</h4>
      <div className="form-grid">
        <label>
          Action
          <select value={shareAction} onChange={(e) => setShareAction(e.target.value as ShareAction)}>
            <option value="adopt">Adopt held shares</option>
            <option value="release">Release shares</option>
            <option value="adjust">Set tally</option>
          </select>
          <span className="muted small">{SHARE_HELP[shareAction]}</span>
        </label>
        <label>
          Ticker and {shareAction === "adjust" ? "new share count" : "shares"}
          <span className="inline">
            <input className="num wide" placeholder="SPY" value={ticker} onChange={(e) => setTicker(e.target.value.toUpperCase())} />
            <input className="amount" placeholder="Shares" inputMode="decimal" value={qty} onChange={(e) => setQty(e.target.value.replace(/[^0-9.]/g, ""))} />
          </span>
        </label>
      </div>
      <div className="btn-row">
        <button
          className="btn"
          disabled={!shareValid || !!busy || disabled}
          onClick={async () => {
            if (shareAction === "adjust" && !window.confirm(`Set this strategy's ${ticker.trim()} tally to ${qty} shares?`)) return;
            if (await run("shares", { action: shareAction, ticker: ticker.trim(), qty })) {
              setTicker("");
              setQty("");
            }
          }}
        >
          {busy === "shares" ? "Applying…" : "Apply"}
        </button>
      </div>

      <h4 className="small">Sizing</h4>
      <div className="kv">
        <span>Sizes against</span>
        <b>{describeAmount(policy.base, "its own NAV (compounding)")}</b>
      </div>
      <div className="kv">
        <span>Cap</span>
        <b>{describeAmount(policy.cap, "none")}</b>
      </div>
      <div className="form-grid">
        <label>
          Base
          <span className="inline">
            <select value={baseMode} onChange={(e) => setBaseMode(e.target.value as typeof baseMode)}>
              <option value="own-nav">Own NAV (compounding)</option>
              <option value="pct">% of account NAV</option>
              <option value="usd">Fixed dollars</option>
            </select>
            {baseMode !== "own-nav" && (
              <input className="amount" placeholder={baseMode === "pct" ? "20" : "50000"} inputMode="decimal" value={baseValue} onChange={(e) => setBaseValue(e.target.value.replace(/[^0-9.]/g, ""))} />
            )}
            <button
              className="btn small"
              disabled={!amountOk(baseMode, baseValue) || !!busy || disabled}
              onClick={() => run("base", { action: "base", spec: spec(baseMode, baseValue) })}
            >
              Set
            </button>
          </span>
          <span className="muted small">
            {baseMode === "own-nav"
              ? "The strategy grows and shrinks with its own results."
              : baseMode === "pct"
                ? "The strategy's capital floats with the whole account."
                : "Constant dollars: gains above it are trimmed, drawdowns topped up from the account."}
          </span>
        </label>
        <label>
          Cap
          <span className="inline">
            <select value={capMode} onChange={(e) => setCapMode(e.target.value as typeof capMode)}>
              <option value="off">No cap</option>
              <option value="pct">% of account NAV</option>
              <option value="usd">Dollars</option>
            </select>
            {capMode !== "off" && (
              <input className="amount" placeholder={capMode === "pct" ? "25" : "50000"} inputMode="decimal" value={capValue} onChange={(e) => setCapValue(e.target.value.replace(/[^0-9.]/g, ""))} />
            )}
            <button
              className="btn small"
              disabled={!amountOk(capMode, capValue) || !!busy || disabled}
              onClick={() => run("cap", { action: "cap", spec: spec(capMode, capValue) })}
            >
              Set
            </button>
          </span>
          <span className="muted small">The most the strategy may ever deploy; gains beyond it stay in its cash.</span>
        </label>
      </div>

      {result && <pre className={`alert small ${result.ok ? "" : "error"}`} style={{ whiteSpace: "pre-wrap", overflowX: "auto" }}>{result.output}</pre>}
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

/** Optional number: blank means "use the default" (null). */
function OptNum({
  value,
  onChange,
  min,
  max,
  step = 1,
  placeholder,
  wide = false,
}: {
  value: number | null;
  onChange: (v: number | null) => void;
  min?: number;
  max?: number;
  step?: number;
  placeholder?: string;
  wide?: boolean;
}) {
  const [text, setText] = useState(value === null ? "" : String(value));
  // Re-sync only when the value changed from outside (e.g. switching
  // strategy), so typing "0." isn't snapped back to "0".
  useEffect(() => {
    setText((t) => ((t.trim() === "" ? null : Number(t)) === value ? t : value === null ? "" : String(value)));
  }, [value]);
  return (
    <input
      type="number"
      className={wide ? "num wide" : "num"}
      value={text}
      min={min}
      max={max}
      step={step}
      placeholder={placeholder}
      onChange={(e) => {
        setText(e.target.value);
        const raw = e.target.value.trim();
        if (raw === "") return onChange(null);
        const v = Number(raw);
        if (Number.isFinite(v) && (min === undefined || v >= min) && (max === undefined || v <= max)) onChange(v);
      }}
    />
  );
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
