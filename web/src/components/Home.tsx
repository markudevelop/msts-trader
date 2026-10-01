import { useMemo, useState } from "react";
import { api, apiBlob, download } from "../api";
import type { DashRow } from "../types";

const pct = (v: number | null | undefined, dp = 1) => (v == null ? "–" : `${(v * 100).toFixed(dp)}%`);
const num = (v: number | null | undefined) => (v == null ? "–" : v.toFixed(2));
const money = (v: string | number | null | undefined) =>
  v == null || v === "" ? "–" : `$${Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
const day = (ts: string | null | undefined) => (ts ? ts.slice(0, 10) : "–");

type Sort = "name" | "cagr" | "sharpe" | "mdd" | "viewed" | "capital";

function schedule(r: DashRow) {
  if (!r.deploy.schedule_enabled) return <span className="muted" title="no automatic runs (manual only)">off</span>;
  return (
    <span>
      ⏱ {r.deploy.schedule_time} ET <span className="muted">· {r.rebalance}</span>
    </span>
  );
}

function RunCell({ r }: { r: DashRow }) {
  const lr = r.last_run;
  if (!lr) return <span className="muted">–</span>;
  return (
    <span title={lr.error ?? ""}>
      <span className={`status status-${lr.status}`}>{lr.status}</span> <span className="muted small">{day(lr.ts)}</span>
    </span>
  );
}

function Tags({ tags }: { tags: string[] }) {
  return (
    <>
      {tags.map((t) => (
        <span className="tag-chip" key={t}>
          {t}
        </span>
      ))}
    </>
  );
}

export function Home({
  rows,
  onOpen,
  onChanged,
  onNew,
  onImport,
}: {
  rows: DashRow[];
  onOpen: (id: string) => void;
  onChanged: () => Promise<void>;
  onNew: () => void;
  onImport: () => void;
}) {
  const funded = rows.filter((r) => r.funded);
  // Default to Funded once data is in (rows arrive after the first render).
  const [picked, setView] = useState<"funded" | "all" | null>(null);
  const view = picked ?? (funded.length ? "funded" : "all");

  if (!rows.length) {
    return (
      <div className="welcome">
        <h1>Build strategies. Backtest them. Trade them on your own broker.</h1>
        <p className="muted">
          Compose blocks such as weights, if/else on indicators and top-N filters into a strategy, or import one from Composer or a feed. msts-trader's
          rebalancer then executes it in an isolated sleeve of your account.
        </p>
        <div className="btn-row">
          <button className="btn primary" onClick={onNew}>
            Start from a template
          </button>
          <button className="btn" onClick={onImport}>
            Import
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="panel-stack">
      <div className="home-head">
        <h2>Strategies</h2>
        <div className="tabs compact" role="tablist">
          <button role="tab" aria-selected={view === "funded"} className={`tab ${view === "funded" ? "active" : ""}`} onClick={() => setView("funded")}>
            Funded ({funded.length})
          </button>
          <button role="tab" aria-selected={view === "all"} className={`tab ${view === "all" ? "active" : ""}`} onClick={() => setView("all")}>
            All strategies ({rows.length})
          </button>
        </div>
      </div>
      {view === "funded" ? <Funded rows={funded} onOpen={onOpen} /> : <Library rows={rows} onOpen={onOpen} onChanged={onChanged} />}
    </div>
  );
}

function Funded({ rows, onOpen }: { rows: DashRow[]; onOpen: (id: string) => void }) {
  const total = rows.reduce((a, r) => a + Number(r.contributed ?? 0), 0);
  if (!rows.length) {
    return (
      <div className="empty-state">
        No funded strategies yet. Open a strategy, go to <b>Deploy</b> and <b>Invest</b> some capital. It will show up here with its stats.
      </div>
    );
  }
  return (
    <div className="card-plain">
      <div className="section-head">
        <span className="muted small">
          {rows.length} funded · {money(total)} allocated · stats are from each strategy's last full backtest
        </span>
      </div>
      <div className="table-scroll">
        <table className="table dash">
          <thead>
            <tr>
              <th>Strategy</th>
              <th className="num">Capital</th>
              <th className="num">Cash</th>
              <th className="num">Pos.</th>
              <th>Schedule</th>
              <th>Last run</th>
              <th className="num">CAGR</th>
              <th className="num">Max DD</th>
              <th className="num">Sharpe</th>
              <th>Backtest</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const m = r.last_backtest?.metrics;
              return (
                <tr key={r.id} className="clickable" onClick={() => onOpen(r.id)}>
                  <td>
                    <b>{r.name}</b> {r.deploy.live_enabled && <span className="pill pill-live">live</span>} <span className="muted small">{r.deploy.broker}</span>
                    <div>
                      <Tags tags={r.tags} />
                    </div>
                  </td>
                  <td className="num mono">{money(r.contributed)}</td>
                  <td className="num mono">{money(r.cash)}</td>
                  <td className="num mono">{r.positions}</td>
                  <td className="small">{schedule(r)}</td>
                  <td>
                    <RunCell r={r} />
                  </td>
                  <td className="num mono">{pct(m?.cagr)}</td>
                  <td className="num mono">{pct(m?.max_drawdown)}</td>
                  <td className="num mono">{num(m?.sharpe)}</td>
                  <td className="muted small">{r.last_backtest ? `${r.last_backtest.start.slice(0, 4)}–${r.last_backtest.end.slice(0, 4)} · ${day(r.last_backtest.ts)}` : "not run"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Library({ rows, onOpen, onChanged }: { rows: DashRow[]; onOpen: (id: string) => void; onChanged: () => Promise<void> }) {
  const [q, setQ] = useState("");
  const [tag, setTag] = useState<string>("*");
  const [sort, setSort] = useState<Sort>("viewed");
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [cashOpen, setCashOpen] = useState(false);

  const allTags = useMemo(() => [...new Set(rows.flatMap((r) => r.tags))].sort((a, b) => a.localeCompare(b)), [rows]);
  const shown = useMemo(() => {
    const ql = q.trim().toLowerCase();
    const out = rows.filter(
      (r) =>
        (!ql || r.name.toLowerCase().includes(ql) || r.id.includes(ql) || r.tags.some((t) => t.toLowerCase().includes(ql))) &&
        (tag === "*" || (tag === "" ? r.tags.length === 0 : r.tags.includes(tag))),
    );
    const metric = (r: DashRow, k: "cagr" | "sharpe" | "max_drawdown") => r.last_backtest?.metrics?.[k] ?? null;
    const desc = (a: number | null, b: number | null) => (b ?? -Infinity) - (a ?? -Infinity);
    out.sort((a, b) => {
      if (sort === "name") return a.name.localeCompare(b.name);
      if (sort === "cagr") return desc(metric(a, "cagr"), metric(b, "cagr"));
      if (sort === "sharpe") return desc(metric(a, "sharpe"), metric(b, "sharpe"));
      if (sort === "mdd") return (metric(a, "max_drawdown") ?? Infinity) - (metric(b, "max_drawdown") ?? Infinity);
      if (sort === "capital") return Number(b.contributed ?? 0) - Number(a.contributed ?? 0);
      return (b.last_viewed ?? "").localeCompare(a.last_viewed ?? "");
    });
    return out;
  }, [rows, q, tag, sort]);

  const ids = [...sel].filter((id) => rows.some((r) => r.id === id));
  const allOn = shown.length > 0 && shown.every((r) => sel.has(r.id));
  const toggle = (id: string) => setSel((s) => (s.has(id) ? new Set([...s].filter((x) => x !== id)) : new Set([...s, id])));

  const bulk = async (action: string, extra: Record<string, unknown> = {}) => {
    setBusy(action);
    setMsg(null);
    try {
      const r = await api<{ done: string[]; failed: { id: string; error: string }[] }>("/strategies/bulk", { body: { ids, action, ...extra } });
      setMsg(`${action}: ${r.done.length} done${r.failed.length ? `, ${r.failed.length} failed (${r.failed[0].error})` : ""}`);
      if (action === "delete") setSel(new Set());
      await onChanged();
    } catch (e) {
      setMsg((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const doTag = () => {
    const t = window.prompt(`Tag ${ids.length} strateg${ids.length === 1 ? "y" : "ies"} as:`, tag !== "*" && tag ? tag : "");
    if (t && t.trim()) bulk("tag", { tag: t.trim() });
  };
  const doUntag = () => {
    const t = window.prompt(`Remove which tag from ${ids.length} strateg${ids.length === 1 ? "y" : "ies"}?`, tag !== "*" && tag ? tag : allTags[0] ?? "");
    if (t && t.trim()) bulk("untag", { tag: t.trim() });
  };
  const doDelete = () => {
    if (window.confirm(`Delete ${ids.length} strateg${ids.length === 1 ? "y" : "ies"}? Holdings at the broker are not touched (use Go to cash first).`)) bulk("delete");
  };
  const doExport = async () => {
    setBusy("export");
    try {
      download(await apiBlob("/strategies/export", { ids }), "msts-strategies.zip");
    } catch (e) {
      setMsg((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="card-plain">
      <div className="toolbar flat">
        <input className="grow" placeholder={`Search ${rows.length} strategies…`} value={q} onChange={(e) => setQ(e.target.value)} />
        <label className="inline small">
          Tag
          <select value={tag} onChange={(e) => setTag(e.target.value)}>
            <option value="*">all</option>
            <option value="">untagged</option>
            {allTags.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>
        <label className="inline small">
          Sort
          <select value={sort} onChange={(e) => setSort(e.target.value as Sort)}>
            <option value="viewed">recently viewed</option>
            <option value="name">name</option>
            <option value="cagr">CAGR</option>
            <option value="sharpe">Sharpe</option>
            <option value="mdd">max drawdown</option>
            <option value="capital">capital</option>
          </select>
        </label>
      </div>
      {ids.length > 0 && (
        <div className="bulk-bar">
          <b>{ids.length} selected</b>
          <button className="btn small" disabled={!!busy} onClick={doTag}>
            Tag…
          </button>
          <button className="btn small" disabled={!!busy || !allTags.length} onClick={doUntag}>
            Untag…
          </button>
          <button className="btn small" disabled={!!busy} onClick={() => bulk("pause")}>
            Pause
          </button>
          <button className="btn small" disabled={!!busy} onClick={() => bulk("resume")}>
            Resume
          </button>
          <button className="btn small" disabled={!!busy} onClick={doExport}>
            {busy === "export" ? "Exporting…" : "Export JSON"}
          </button>
          <button className="btn small danger" disabled={!!busy} onClick={() => setCashOpen(true)}>
            Go to cash…
          </button>
          <button className="btn small ghost danger-text" disabled={!!busy} onClick={doDelete}>
            Delete
          </button>
          <span className="spacer" />
          <button className="btn small ghost" onClick={() => setSel(new Set())}>
            Clear
          </button>
        </div>
      )}
      {msg && <div className="alert warn small">{msg}</div>}
      <div className="table-scroll">
        <table className="table dash">
          <thead>
            <tr>
              <th>
                <input
                  type="checkbox"
                  aria-label="Select all shown"
                  checked={allOn}
                  onChange={() => setSel(allOn ? new Set([...sel].filter((id) => !shown.some((r) => r.id === id))) : new Set([...sel, ...shown.map((r) => r.id)]))}
                />
              </th>
              <th>Strategy</th>
              <th className="num">CAGR</th>
              <th className="num">Max DD</th>
              <th className="num">Sharpe</th>
              <th className="num">Capital</th>
              <th>Schedule</th>
              <th>Last run</th>
              <th>Viewed</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((r) => {
              const m = r.last_backtest?.metrics;
              return (
                <tr key={r.id} className={`clickable ${sel.has(r.id) ? "selected" : ""}`} onClick={() => onOpen(r.id)}>
                  <td onClick={(e) => e.stopPropagation()}>
                    <input type="checkbox" aria-label={`Select ${r.name}`} checked={sel.has(r.id)} onChange={() => toggle(r.id)} />
                  </td>
                  <td>
                    {r.name} {r.funded && <span className="pill pill-oos">funded</span>} <Tags tags={r.tags} />
                  </td>
                  <td className="num mono">{pct(m?.cagr)}</td>
                  <td className="num mono">{pct(m?.max_drawdown)}</td>
                  <td className="num mono">{num(m?.sharpe)}</td>
                  <td className="num mono">{r.funded ? money(r.contributed) : "–"}</td>
                  <td className="small">{schedule(r)}</td>
                  <td>
                    <RunCell r={r} />
                  </td>
                  <td className="muted small">{day(r.last_viewed)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {!shown.length && <p className="muted small">No strategies match.</p>}
      {cashOpen && (
        <CashModal
          rows={rows.filter((r) => ids.includes(r.id))}
          onClose={() => setCashOpen(false)}
          onDone={async (text) => {
            setCashOpen(false);
            setMsg(text);
            await onChanged();
          }}
        />
      )}
    </div>
  );
}

export function CashModal({ rows, onClose, onDone }: { rows: DashRow[]; onClose: () => void; onDone: (summary: string) => Promise<void> }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const real = rows.filter((r) => r.deploy.broker !== "paper");
  const go = async () => {
    setBusy(true);
    setErr(null);
    try {
      const r = await api<{ results: { strategy: string; status: string; error?: string }[] }>("/strategies/cash", {
        body: { ids: rows.map((x) => x.id), confirm: text },
      });
      await onDone(r.results.map((x) => `${x.strategy}: ${x.status}${x.error ? ` (${x.error})` : ""}`).join(" · "));
    } catch (e) {
      setErr((e as Error).message);
      setBusy(false);
    }
  };
  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div className="modal" role="dialog" aria-modal="true" onMouseDown={(e) => e.stopPropagation()}>
        <h3>Go to cash</h3>
        <p>
          Sells everything held by {rows.length === 1 ? <b>{rows[0].name}</b> : <b>{rows.length} strategies</b>} and pauses their schedules. Only each
          strategy's own sleeve is sold; your other positions are untouched.
        </p>
        {real.length > 0 && (
          <div className="alert error small">
            Real orders on {[...new Set(real.map((r) => r.deploy.broker))].join(", ")} for {real.length} strateg{real.length === 1 ? "y" : "ies"}.
          </div>
        )}
        <p className="small">
          Type <code>CASH</code> to confirm.
        </p>
        <input autoFocus value={text} onChange={(e) => setText(e.target.value)} onKeyDown={(e) => e.key === "Enter" && text === "CASH" && go()} />
        {err && <div className="alert error small">{err}</div>}
        <div className="btn-row end">
          <button className="btn ghost" onClick={onClose}>
            Cancel
          </button>
          <button className="btn danger" disabled={text !== "CASH" || busy} onClick={go}>
            {busy ? "Selling…" : "Go to cash"}
          </button>
        </div>
      </div>
    </div>
  );
}
