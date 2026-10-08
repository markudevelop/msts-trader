import { Fragment, useMemo, useState, type ReactNode } from "react";
import { api, apiBlob, download } from "../api";
import type { DashRow, Rollup, Stage } from "../types";

const pct = (v: number | null | undefined, dp = 1) => (v == null ? "–" : `${(v * 100).toFixed(dp)}%`);
const num = (v: number | null | undefined) => (v == null ? "–" : v.toFixed(2));
const money = (v: string | number | null | undefined) =>
  v == null || v === "" ? "–" : `$${Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
const day = (ts: string | null | undefined) => (ts ? ts.slice(0, 10) : "–");
const when = (ts: string | null | undefined) => {
  if (!ts) return "–";
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? ts : d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
};

type Sort = "name" | "cagr" | "sharpe" | "mdd" | "viewed" | "capital";

function schedule(r: DashRow) {
  if (!r.deploy.schedule_enabled) return <span className="muted" title="no automatic runs (manual only)">off</span>;
  return (
    <span>
      ⏱ {r.deploy.schedule_time} ET <span className="muted">· {r.rebalance}</span>
      {r.deploy.order_type === "moc" && (
        <span className="muted" title="market-on-close orders (fill in the closing auction)">
          {" "}
          · MOC
        </span>
      )}
      {r.deploy.order_type === "limit-chase" && (
        <span className="muted" title="limit-chase orders (limit near the mid, then market)">
          {" "}
          · chase
        </span>
      )}
      {r.deploy.order_type === "extended" && (
        <span className="muted" title="extended hours: limit-only orders, premarket and after-hours allowed">
          {" "}
          · ext
        </span>
      )}
    </span>
  );
}

/** The last rebalance check (scheduled or manual) — not a backtest. */
function RunCell({ r }: { r: DashRow }) {
  const lr = r.last_run;
  if (!lr) return <span className="muted">never</span>;
  const label = lr.target === "cash" ? "went to cash" : lr.status === "preview" ? "preview only" : lr.status;
  const detail =
    lr.status === "preview"
      ? `${lr.orders} order(s) previewed, none placed (live orders are off)`
      : lr.status === "executed"
        ? `${lr.orders} order(s) placed`
        : lr.error ?? lr.status;
  return (
    <span title={`${lr.source === "scheduler" ? "Scheduled" : "Manual"} check · ${detail}`}>
      <span className={`status status-${lr.status}`}>{label}</span> <span className="muted small">{when(lr.ts)}</span>
    </span>
  );
}

// "live" = real money; a paper / sandbox strategy that places (simulated) orders is "paper".
export function StagePill({ r }: { r: DashRow }) {
  if (!r.deploy.live_enabled) return null;
  return r.stage === "live" ? (
    <span className="pill pill-live" title="places real-money orders">
      live
    </span>
  ) : (
    <span className="pill pill-paper" title="places paper / sandbox orders: no real money">
      paper
    </span>
  );
}

function ValueCell({ r }: { r: DashRow }) {
  if (r.nav != null) return <>{money(r.nav)}</>;
  const missing = r.unpriced ?? [];
  if (missing.length) {
    return (
      <span
        className="muted"
        title={`No cached price yet for ${missing.join(", ")}, so the value can't be computed. Prices refresh after each live run, or open the strategy's Performance tab.`}
      >
        – <span className="warn-text">⚠</span>
      </span>
    );
  }
  return <>{money(r.cash)}</>; // nothing held: the value is the sleeve's cash
}

function HeldCell({ r }: { r: DashRow }) {
  const t = r.target_positions;
  const nb = r.not_bought ?? [];
  const whole = r.sizing?.whole_shares;
  const why = nb.map((x) => `${x.ticker} (${(Number(x.target_pct) * 100).toFixed(2)}%): ${x.note}`).join("\n");
  const hint = whole
    ? r.deploy.order_type === "moc"
      ? "\n\nMarket-on-close orders are whole shares. Switch the order type to Market or add capital."
      : "\n\nThis run sized to whole shares: the broker can't trade fractions."
    : "";
  return (
    <span title={`holds ${r.positions} ticker(s)${t != null ? `; latest target has ${t}` : ""}`}>
      <b>{r.positions}</b>
      <span className="muted"> / {t ?? "–"}</span>
      {nb.length > 0 && (
        <div className="small warn-text" title={`Not bought on the latest run:\n${why}${hint}`}>
          {nb.length} not bought
        </div>
      )}
    </span>
  );
}

function LiveCells({ r }: { r: DashRow }) {
  const l = r.live;
  if (!l) {
    return (
      <>
        <td className="muted small" title="out-of-sample tracking starts at the first executed live (or paper) run">
          not live yet
        </td>
        <td className="num mono muted">–</td>
        <td className="num mono muted">–</td>
        <td className="num mono muted">–</td>
      </>
    );
  }
  const short = l.cagr == null && l.total_return != null;
  return (
    <>
      <td className="small mono" title={l.reason ?? `out-of-sample since go-live · ${l.days ?? 0} trading day(s)${l.asof ? ` · marked at the ${l.asof} close` : ""}`}>
        {l.since}
        {l.reason && <span className="warn-text"> ⚠</span>}
      </td>
      <td
        className="num mono"
        title={short ? "under 30 days live: an annualised CAGR would be noise, so this is the total return since go-live" : "annualised, time-weighted (deposits/withdrawals excluded)"}
      >
        {short ? (
          <>
            {pct(l.total_return)}
            <span className="muted small"> total</span>
          </>
        ) : (
          pct(l.cagr)
        )}
      </td>
      <td className="num mono">{pct(l.max_drawdown)}</td>
      <td className="num mono" title={l.sharpe == null && l.total_return != null ? "needs 30+ days live" : "daily returns, rf = 0, annualised"}>
        {num(l.sharpe)}
      </td>
    </>
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

type View = { kind: "stage"; stage: Stage } | { kind: "tag"; tag: string } | { kind: "all" };
const sameView = (a: View, b: View) =>
  a.kind === b.kind && (a.kind !== "stage" || a.stage === (b as typeof a).stage) && (a.kind !== "tag" || a.tag === (b as typeof a).tag);
const VIEW_KEY = "msts.home.view";

function loadView(): View | null {
  try {
    const v = JSON.parse(localStorage.getItem(VIEW_KEY) || "null");
    return v && typeof v.kind === "string" ? (v as View) : null;
  } catch {
    return null;
  }
}

export function Home({
  rows,
  rollups,
  homeTabs,
  onHomeTabs,
  onOpen,
  onChanged,
  onNew,
  onImport,
}: {
  rows: DashRow[];
  rollups: Record<Stage, Rollup> | null;
  homeTabs: string[];
  onHomeTabs: (tabs: string[]) => Promise<void>;
  onOpen: (id: string) => void;
  onChanged: () => Promise<void>;
  onNew: () => void;
  onImport: () => void;
}) {
  const live = rows.filter((r) => r.funded && r.stage === "live");
  const incubation = rows.filter((r) => r.funded && r.stage === "incubation");
  const allTags = useMemo(() => [...new Set(rows.flatMap((r) => r.tags))].sort((a, b) => a.localeCompare(b)), [rows]);
  const [picked, setPicked] = useState<View | null>(loadView);
  const setView = (v: View) => {
    setPicked(v);
    try {
      localStorage.setItem(VIEW_KEY, JSON.stringify(v));
    } catch {
      /* private window: the tab just isn't remembered */
    }
  };
  // Default once data is in: real money first, then paper, then the library.
  const fallback: View = live.length ? { kind: "stage", stage: "live" } : incubation.length ? { kind: "stage", stage: "incubation" } : { kind: "all" };
  const view: View = picked && (picked.kind !== "tag" || homeTabs.includes(picked.tag)) ? picked : fallback;
  const [adding, setAdding] = useState(false);
  const unpinned = allTags.filter((t) => !homeTabs.includes(t));
  const tab = (v: View, label: string, count: number, extra?: ReactNode) => (
    <button key={JSON.stringify(v)} role="tab" aria-selected={sameView(view, v)} className={`tab ${sameView(view, v) ? "active" : ""}`} onClick={() => setView(v)}>
      {label} ({count}){extra}
    </button>
  );

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
        <div className="tabs compact home-tabs" role="tablist">
          {tab({ kind: "stage", stage: "live" }, "Live", live.length)}
          {tab({ kind: "stage", stage: "incubation" }, "Incubation", incubation.length)}
          {homeTabs.map((t) =>
            tab(
              { kind: "tag", tag: t },
              t,
              rows.filter((r) => r.tags.includes(t)).length,
              <span
                className="tab-x"
                role="button"
                aria-label={`Unpin ${t}`}
                title="Unpin this tab (the tag stays on its strategies)"
                onClick={(e) => {
                  e.stopPropagation();
                  void onHomeTabs(homeTabs.filter((x) => x !== t));
                }}
              >
                ×
              </span>,
            ),
          )}
          {tab({ kind: "all" }, "All strategies", rows.length)}
          {adding ? (
            <select
              className="tab-add"
              autoFocus
              value=""
              onBlur={() => setAdding(false)}
              onChange={(e) => {
                const t = e.target.value;
                setAdding(false);
                if (t) {
                  void onHomeTabs([...homeTabs, t]);
                  setView({ kind: "tag", tag: t });
                }
              }}
            >
              <option value="">Pin a tag as a tab…</option>
              {unpinned.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          ) : (
            <button
              className="tab tab-plus"
              disabled={!unpinned.length}
              title={unpinned.length ? "Pin a tag as a tab (e.g. options, active, passive)" : "Add tags to strategies first (under each strategy's name), then pin them here"}
              onClick={() => setAdding(true)}
            >
              +
            </button>
          )}
        </div>
      </div>
      {view.kind === "stage" ? (
        <>
          <Funded rows={view.stage === "live" ? live : incubation} stage={view.stage} onOpen={onOpen} />
          {rollups && (view.stage === "live" ? live : incubation).length > 0 && <Combined rollup={rollups[view.stage]} onOpen={onOpen} />}
        </>
      ) : view.kind === "tag" ? (
        <TagView tag={view.tag} rows={rows.filter((r) => r.tags.includes(view.tag))} onOpen={onOpen} onChanged={onChanged} />
      ) : (
        <Library rows={rows} onOpen={onOpen} onChanged={onChanged} />
      )}
    </div>
  );
}

function TagView({ tag, rows, onOpen, onChanged }: { tag: string; rows: DashRow[]; onOpen: (id: string) => void; onChanged: () => Promise<void> }) {
  const funded = rows.filter((r) => r.funded);
  const rest = rows.filter((r) => !r.funded);
  if (!rows.length) {
    return <div className="empty-state">No strategy is tagged “{tag}” any more. Unpin the tab with its ×, or tag a strategy under its name.</div>;
  }
  return (
    <>
      {funded.length > 0 && <Funded rows={funded} onOpen={onOpen} />}
      {rest.length > 0 && <Library rows={rest} onOpen={onOpen} onChanged={onChanged} />}
    </>
  );
}

function Funded({ rows, stage, onOpen }: { rows: DashRow[]; stage?: Stage; onOpen: (id: string) => void }) {
  const total = rows.reduce((a, r) => a + Number(r.contributed ?? 0), 0);
  if (!rows.length) {
    return (
      <div className="empty-state">
        {stage === "live" ? (
          <>
            No strategies trading real money yet. Paper, sandbox and preview-only strategies are under <b>Incubation</b>. To go live, pick a real broker
            in a strategy's <b>Deploy</b> tab, allow live orders and <b>Invest</b> capital.
          </>
        ) : stage === "incubation" ? (
          <>
            Nothing in incubation. Open a strategy, go to <b>Deploy</b>, keep the <b>paper</b> broker and <b>Invest</b> some capital to paper-trade it and
            build an out-of-sample record.
          </>
        ) : (
          <>
            No funded strategies yet. Open a strategy, go to <b>Deploy</b> and <b>Invest</b> some capital. It will show up here with its stats.
          </>
        )}
      </div>
    );
  }
  const previewOnly = rows.filter((r) => !r.deploy.live_enabled);
  return (
    <div className="card-plain">
      <div className="section-head">
        <span className="muted small">
          {rows.length} {stage === "live" ? "trading real money" : stage === "incubation" ? "in incubation (paper / preview-only)" : "funded"} ·{" "}
          {money(total)} allocated · Out-of-sample = the sleeve's own record since go-live · Backtest = the last full backtest
        </span>
      </div>
      {previewOnly.length > 0 && (
        <div className="alert warn small">
          <b>{previewOnly.length === rows.length ? "All" : previewOnly.length} of these strateg{previewOnly.length === 1 ? "y is" : "ies are"} preview-only.</b> "Allow
          live orders" is off, so each scheduled check only previews the orders and places none, which is why <b>Held</b> stays at 0. Turn it on in the
          strategy's Deploy tab to trade (paper is simulated; real brokers place real orders).
        </div>
      )}
      <div className="table-scroll">
        <table className="table dash">
          <thead>
            <tr className="group-head">
              <th colSpan={6} />
              <th colSpan={4} className="group" title="actual sleeve performance since go-live (out-of-sample), time-weighted">
                Out-of-sample
              </th>
              <th colSpan={4} className="group" title="the strategy's last full backtest (in-sample)">
                Backtest
              </th>
            </tr>
            <tr>
              <th>Strategy</th>
              <th className="num">Capital</th>
              <th className="num" title="cash + holdings at the latest cached close">Value</th>
              <th className="num" title="tickers held now / tickers in the latest target">Held / target</th>
              <th>Schedule</th>
              <th title="the last rebalance check, scheduled or manual">Last check</th>
              <th className="group-start" title="out-of-sample start: the first executed live run">
                Since
              </th>
              <th className="num">CAGR</th>
              <th className="num">Max DD</th>
              <th className="num">Sharpe</th>
              <th className="num group-start">CAGR</th>
              <th className="num">Max DD</th>
              <th className="num">Sharpe</th>
              <th>Period</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const m = r.last_backtest?.metrics;
              return (
                <tr key={r.id} className="clickable" onClick={() => onOpen(r.id)}>
                  <td>
                    <b>{r.name}</b> <StagePill r={r} />{" "}
                    <span className="muted small">
                      {r.deploy.broker}
                      {r.deploy.paper_account ? " · paper account" : ""}
                    </span>
                    <div>
                      <Tags tags={r.tags} />
                    </div>
                  </td>
                  <td className="num mono">{money(r.contributed)}</td>
                  <td className="num mono">
                    <ValueCell r={r} />
                  </td>
                  <td className="num mono">
                    <HeldCell r={r} />
                  </td>
                  <td className="small">{schedule(r)}</td>
                  <td>
                    <RunCell r={r} />
                  </td>
                  <LiveCells r={r} />
                  <td className="num mono group-start">{pct(m?.cagr)}</td>
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
              <th title="the last rebalance check, scheduled or manual">Last check</th>
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

/** Combined portfolio: every funded strategy's latest target x its capital, by ticker. */
function Combined({ rollup, onOpen }: { rollup: Rollup; onOpen: (id: string) => void }) {
  const [open, setOpen] = useState<string | null>(null);
  const anyHeld = rollup.total_held > 0;
  if (!rollup.tickers.length) {
    return (
      <div className="card-plain">
        <h3>Combined portfolio</h3>
        <p className="muted small">Appears after the funded strategies' first rebalance check (Preview orders, or the scheduled run).</p>
      </div>
    );
  }
  return (
    <div className="card-plain">
      <div className="section-head">
        <h3>Combined portfolio</h3>
        <span className="muted small">
          target {money(rollup.total_target)} of {money(rollup.total_capital)}
          {rollup.unallocated > 0.5 ? ` · ${money(rollup.unallocated)} held as cash by design` : ""}
          {anyHeld ? ` · held now ${money(rollup.total_held)}` : " · nothing held yet"}
        </span>
      </div>
      <p className="muted small">
        Each funded strategy's latest target ({rollup.with_targets} of {rollup.strategies} have one) times its capital, summed by ticker. Click a row
        to see which strategies hold it.
      </p>
      <div className="table-scroll">
        <table className="table dash">
          <thead>
            <tr>
              <th>Ticker</th>
              <th className="num">Target $</th>
              <th className="num">Target %</th>
              <th className="weight-bar-col" />
              <th className="num">Held $</th>
              <th className="num">Held shares</th>
              <th className="num">Strategies</th>
            </tr>
          </thead>
          <tbody>
            {rollup.tickers.map((t) => (
              <Fragment key={t.ticker}>
                <tr className="clickable" onClick={() => setOpen(open === t.ticker ? null : t.ticker)}>
                  <td className="mono">
                    <b>{t.ticker}</b>
                  </td>
                  <td className="num mono">{money(t.target_value)}</td>
                  <td className="num mono">{pct(t.target_weight)}</td>
                  <td className="weight-bar-col">
                    <div className="bar">
                      <div style={{ width: `${Math.min(100, (t.target_weight ?? 0) * 100)}%` }} />
                    </div>
                  </td>
                  <td className="num mono">{t.held_qty ? (t.priced ? money(t.held_value) : "no price") : "–"}</td>
                  <td className="num mono">{t.held_qty ? t.held_qty.toLocaleString(undefined, { maximumFractionDigits: 4 }) : "–"}</td>
                  <td className="num">{t.by.length}</td>
                </tr>
                {open === t.ticker && (
                  <tr className="breakdown">
                    <td />
                    <td colSpan={6}>
                      {t.by
                        .slice()
                        .sort((a, b) => b.value - a.value)
                        .map((b) => (
                          <button key={b.id} className="chip link" onClick={() => onOpen(b.id)}>
                            {b.name} <b>{money(b.value)}</b> <span className="muted">({pct(b.weight)} of it)</span>
                          </button>
                        ))}
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
