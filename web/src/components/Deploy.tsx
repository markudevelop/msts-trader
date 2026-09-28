import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { Deploy, Meta, RunEntry, SchedulerState, SleeveLedger, Strategy } from "../types";
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

  const refresh = useCallback(async () => {
    const [r, s, sc] = await Promise.all([
      api<RunEntry[]>(`/runs?strategy=${encodeURIComponent(saved.id)}&limit=20`),
      api<{ ledgers: SleeveLedger[] }>(`/strategies/${saved.id}/sleeve`),
      api<SchedulerState>("/scheduler"),
    ]);
    setRuns(r);
    setSleeve(s.ledgers);
    setSched(sc);
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

  return (
    <div className="deploy-grid">
      <div className="panel-stack">
        <section className="card-plain">
          <h3>Broker &amp; schedule</h3>
          <div className="form-grid">
            <label>
              Broker
              <select value={d.broker} onChange={(e) => set({ broker: e.target.value, live_enabled: false })}>
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
            <label className="check">
              <input type="checkbox" checked={d.schedule_enabled} onChange={(e) => set({ schedule_enabled: e.target.checked })} />
              Run automatically at
              <input type="time" value={d.schedule_time} disabled={!d.schedule_enabled} onChange={(e) => set({ schedule_time: e.target.value })} /> ET,{" "}
              {draft.rebalance}
            </label>
          </div>
          <div className={`live-toggle ${d.live_enabled ? "on" : ""}`}>
            <label className="check">
              <input type="checkbox" checked={d.live_enabled} onChange={(e) => set({ live_enabled: e.target.checked })} />
              <b>Allow live orders on {d.broker}</b>
            </label>
            <p className="muted small">
              {d.broker === "paper"
                ? "Paper trading is simulated locally; no real orders."
                : "Real money. Scheduled runs place orders without asking. Manual runs still need a typed confirmation."}
            </p>
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
          </div>
          {err && <div className="alert error">{err}</div>}
          {last && <RunResult run={last} />}
        </section>

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
          {sched?.upcoming?.filter((u) => u.strategy === saved.id).map((u) => (
            <p className="small" key={u.strategy}>
              Next check <b>{u.next_check.replace("T", " ").slice(0, 16)}</b> ET · {u.mode === "live" ? "LIVE" : "dry-run"} · {u.cadence}
            </p>
          ))}
          <p className="muted small">
            The scheduler only runs while <code>msts-trader ui</code> is open. To trade unattended, schedule this instead (cron, Task Scheduler, or
            GitHub Actions):
          </p>
          <pre className="code">msts-trader strategy run {saved.id} --yes</pre>
        </section>
      </div>
      {confirming && <ConfirmLive strategy={saved} onCancel={() => setConfirming(false)} onConfirm={executeLive} />}
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
