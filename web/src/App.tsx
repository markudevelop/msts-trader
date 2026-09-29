import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError, hasToken } from "./api";
import { TEMPLATES, treeTickers } from "./blocks";
import { BacktestPanel } from "./components/Backtest";
import { DeployPanel } from "./components/Deploy";
import { NodeList } from "./components/Editor";
import type { Cadence, EvalResult, Meta, Strategy, StrategySummary } from "./types";

type Tab = "build" | "backtest" | "deploy";

function parseHash(): { id: string | null; tab: Tab } {
  const [id, tab] = window.location.hash.replace(/^#\/?/, "").split("/");
  return { id: id || null, tab: (["build", "backtest", "deploy"].includes(tab) ? tab : "build") as Tab };
}

export default function App() {
  const [meta, setMeta] = useState<Meta | null>(null);
  const [list, setList] = useState<StrategySummary[]>([]);
  const [route, setRoute] = useState(parseHash);
  const [saved, setSaved] = useState<Strategy | null>(null);
  const [draft, setDraft] = useState<Strategy | null>(null);
  const [fatal, setFatal] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [modal, setModal] = useState<"new" | "import" | null>(null);
  const [saving, setSaving] = useState(false);

  const dirty = useMemo(() => !!draft && !!saved && JSON.stringify(draft) !== JSON.stringify(saved), [draft, saved]);

  const go = useCallback(
    (id: string | null, tab: Tab = "build") => {
      if (dirty && id !== route.id && !window.confirm("Discard unsaved changes?")) return;
      window.location.hash = id ? `/${id}/${tab}` : "";
    },
    [dirty, route.id],
  );

  useEffect(() => {
    const onHash = () => setRoute(parseHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    const warn = (e: BeforeUnloadEvent) => {
      if (dirty) e.preventDefault();
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const loadList = useCallback(async () => setList(await api<StrategySummary[]>("/strategies")), []);

  useEffect(() => {
    if (!hasToken()) {
      setFatal("Open Studio from the link printed by `msts-trader ui`. It carries this session's access token.");
      return;
    }
    Promise.all([api<Meta>("/meta").then(setMeta), loadList()]).catch((e) =>
      setFatal(e instanceof ApiError && e.status === 401 ? "Session token rejected. Reopen the link printed by `msts-trader ui`." : e.message),
    );
    const t = setInterval(() => api<Meta>("/meta").then(setMeta).catch(() => undefined), 60_000);
    return () => clearInterval(t);
  }, [loadList]);

  useEffect(() => {
    if (!route.id) {
      setSaved(null);
      setDraft(null);
      return;
    }
    if (saved?.id === route.id) return;
    api<Strategy>(`/strategies/${route.id}`)
      .then((s) => {
        setSaved(s);
        setDraft(s);
        setErr(null);
      })
      .catch((e) => setErr(e.message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [route.id]);

  const save = useCallback(async () => {
    if (!draft) return;
    setSaving(true);
    setErr(null);
    try {
      const s = await api<Strategy>(`/strategies/${draft.id}`, { method: "PUT", body: draft });
      setSaved(s);
      setDraft(s);
      await loadList();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setSaving(false);
    }
  }, [draft, loadList]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "s") {
        e.preventDefault();
        if (dirty) save();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [dirty, save]);

  const create = async (body: Partial<Strategy>) => {
    const s = await api<Strategy>("/strategies", { body });
    await loadList();
    setSaved(s);
    setDraft(s);
    setModal(null);
    window.location.hash = `/${s.id}/build`;
  };

  const remove = async () => {
    if (!saved || !window.confirm(`Delete "${saved.name}"? (Its sleeve holdings at the broker are not touched.)`)) return;
    await api(`/strategies/${saved.id}`, { method: "DELETE" });
    await loadList();
    setSaved(null);
    setDraft(null);
    window.location.hash = "";
  };

  if (fatal) {
    return (
      <div className="fatal">
        <h1>msts-trader Studio</h1>
        <p>{fatal}</p>
      </div>
    );
  }

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <span className="logo" aria-hidden>
            ◢
          </span>
          <div>
            <div className="brand-name">Studio</div>
            <div className="muted small">msts-trader {meta?.version}</div>
          </div>
        </div>
        <div className="btn-row">
          <button className="btn primary grow" onClick={() => setModal("new")}>
            + New
          </button>
          <button className="btn" onClick={() => setModal("import")}>
            Import
          </button>
        </div>
        <nav className="strategy-list">
          {list.length === 0 && <p className="muted small pad">No strategies yet.</p>}
          {list.map((s) => (
            <button key={s.id} className={`strategy-item ${route.id === s.id ? "active" : ""}`} onClick={() => go(s.id, route.tab)}>
              <div className="strategy-name">{s.name}</div>
              <div className="strategy-meta">
                <span>{s.deploy.broker}</span>
                {s.deploy.live_enabled && <span className="pill pill-live">live</span>}
                {s.deploy.schedule_enabled && <span className="pill">⏱ {s.deploy.schedule_time}</span>}
              </div>
            </button>
          ))}
        </nav>
        <div className={`market market-${meta?.market.status ?? "unknown"}`}>
          <span className="dot" /> Market {meta?.market.status ?? "…"}
        </div>
      </aside>

      <main className="main">
        {!draft || !saved ? (
          <Welcome onNew={() => setModal("new")} onImport={() => setModal("import")} error={err} />
        ) : (
          <>
            <header className="header">
              <div className="title-block">
                <input className="title-input" value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} aria-label="Strategy name" />
                <input
                  className="desc-input"
                  value={draft.description}
                  placeholder="Add a description…"
                  onChange={(e) => setDraft({ ...draft, description: e.target.value })}
                  aria-label="Description"
                />
              </div>
              <div className="header-actions">
                <label className="inline small">
                  Rebalance
                  <select value={draft.rebalance} onChange={(e) => setDraft({ ...draft, rebalance: e.target.value as Cadence })}>
                    {["daily", "weekly", "monthly", "quarterly", "yearly"].map((c) => (
                      <option key={c}>{c}</option>
                    ))}
                  </select>
                </label>
                <button className="btn ghost" onClick={() => create({ ...draft, id: undefined, name: `${draft.name} (copy)` } as Partial<Strategy>)}>
                  Duplicate
                </button>
                <button className="btn ghost danger-text" onClick={remove}>
                  Delete
                </button>
                <button className="btn primary" disabled={!dirty || saving} onClick={save} title="Ctrl+S">
                  {saving ? "Saving…" : dirty ? "Save" : "Saved"}
                </button>
              </div>
            </header>
            <div className="tabs" role="tablist">
              {(["build", "backtest", "deploy"] as Tab[]).map((t) => (
                <button key={t} role="tab" aria-selected={route.tab === t} className={`tab ${route.tab === t ? "active" : ""}`} onClick={() => go(saved.id, t)}>
                  {t === "build" ? "Build" : t === "backtest" ? "Backtest" : "Deploy"}
                </button>
              ))}
              <span className="spacer" />
              <span className="muted small">{treeTickers(draft.children).join(" · ")}</span>
            </div>
            {err && <div className="alert error">{err}</div>}
            <div className="tab-body">
              {route.tab === "build" && <Build draft={draft} setDraft={setDraft} />}
              {route.tab === "backtest" && <BacktestPanel draft={draft} others={list.filter((x) => x.id !== draft.id)} />}
              {route.tab === "deploy" && (
                <DeployPanel draft={draft} saved={saved} dirty={dirty} meta={meta} onDeploy={(deploy) => setDraft({ ...draft, deploy })} onSave={save} />
              )}
            </div>
          </>
        )}
      </main>
      {modal === "new" && <NewModal onClose={() => setModal(null)} onCreate={create} />}
      {modal === "import" && (
        <ImportModal
          onClose={() => setModal(null)}
          onDone={async (s) => {
            await loadList();
            setModal(null);
            setSaved(s);
            setDraft(s);
            window.location.hash = `/${s.id}/build`;
          }}
        />
      )}
    </div>
  );
}

function Build({ draft, setDraft }: { draft: Strategy; setDraft: (s: Strategy) => void }) {
  const [evalRes, setEvalRes] = useState<EvalResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [showJson, setShowJson] = useState(false);
  const run = async () => {
    setBusy(true);
    setErr(null);
    try {
      setEvalRes(await api<EvalResult>("/eval", { body: draft }));
    } catch (e) {
      setErr((e as Error).message);
      setEvalRes(null);
    } finally {
      setBusy(false);
    }
  };
  const total = evalRes ? Object.values(evalRes.weights).reduce((a, b) => a + b, 0) : 0;
  return (
    <div className="build-grid">
      <div className="tree">
        <div className="root-label">
          <span className="tag tag-root">Strategy</span>
          <span className="muted small">top-level blocks share the portfolio equally</span>
        </div>
        <NodeList nodes={draft.children} onChange={(children) => setDraft({ ...draft, children })} parent="root" depth={0} />
      </div>
      <aside className="side-panel">
        <section className="card-plain">
          <div className="section-head">
            <h3>Today's allocation</h3>
            <button className="btn small" onClick={run} disabled={busy}>
              {busy ? "…" : evalRes ? "Refresh" : "Evaluate"}
            </button>
          </div>
          {err && <div className="alert error small">{err}</div>}
          {!evalRes && !err && <p className="muted small">What the strategy would hold on the latest close. Works on unsaved edits.</p>}
          {evalRes && (
            <>
              <p className="muted small">as of {evalRes.asof}</p>
              {Object.entries(evalRes.weights)
                .sort((a, b) => b[1] - a[1])
                .map(([t, w]) => (
                  <div className="bar-row" key={t}>
                    <span className="mono">{t}</span>
                    <div className="bar">
                      <div style={{ width: `${Math.min(100, w * 100)}%` }} />
                    </div>
                    <span className="mono num">{(w * 100).toFixed(1)}%</span>
                  </div>
                ))}
              {total < 0.9999 && (
                <div className="bar-row">
                  <span className="mono muted">cash</span>
                  <div className="bar cash">
                    <div style={{ width: `${(1 - total) * 100}%` }} />
                  </div>
                  <span className="mono num">{((1 - total) * 100).toFixed(1)}%</span>
                </div>
              )}
            </>
          )}
        </section>
        <section className="card-plain">
          <div className="section-head">
            <h3>Definition</h3>
            <button className="btn small ghost" onClick={() => setShowJson(!showJson)}>
              {showJson ? "Hide" : "Show"} JSON
            </button>
          </div>
          {showJson && <pre className="code json">{JSON.stringify({ ...draft, deploy: undefined }, null, 2)}</pre>}
          <p className="muted small">
            Headless: <code>msts-trader strategy eval {draft.id}</code>
          </p>
        </section>
      </aside>
    </div>
  );
}

function Welcome({ onNew, onImport, error }: { onNew: () => void; onImport: () => void; error: string | null }) {
  return (
    <div className="welcome">
      {error && <div className="alert error">{error}</div>}
      <h1>Build strategies. Backtest them. Trade them on your own broker.</h1>
      <p className="muted">
        Compose blocks such as weights, if/else on indicators, and top-N filters into a strategy. Every day it resolves to a set of target weights, and
        msts-trader's rebalancer executes them in an isolated sleeve of your account.
      </p>
      <div className="btn-row">
        <button className="btn primary" onClick={onNew}>
          Start from a template
        </button>
        <button className="btn" onClick={onImport}>
          Import a Composer symphony
        </button>
      </div>
      <ol className="steps">
        <li>
          <b>Build</b>: nest blocks and see today's allocation live.
        </li>
        <li>
          <b>Backtest</b>: compare against SPY, with costs.
        </li>
        <li>
          <b>Deploy</b>: fund a sleeve, preview orders on paper, go live when you're ready.
        </li>
      </ol>
    </div>
  );
}

function NewModal({ onClose, onCreate }: { onClose: () => void; onCreate: (s: Partial<Strategy>) => Promise<void> }) {
  const [err, setErr] = useState<string | null>(null);
  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div className="modal wide" role="dialog" aria-modal="true" onMouseDown={(e) => e.stopPropagation()}>
        <h3>New strategy</h3>
        <div className="templates">
          {TEMPLATES.map((t) => (
            <button key={t.key} className="template" onClick={() => onCreate(t.build()).catch((e) => setErr(e.message))}>
              <b>{t.name}</b>
              <span className="muted small">{t.blurb}</span>
            </button>
          ))}
        </div>
        {err && <div className="alert error">{err}</div>}
        <p className="muted small">Templates are starting points to edit, not recommendations.</p>
      </div>
    </div>
  );
}

function ImportModal({ onClose, onDone }: { onClose: () => void; onDone: (s: Strategy) => Promise<void> }) {
  const [text, setText] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const go = async () => {
    setBusy(true);
    setErr(null);
    try {
      const r = await api<{ strategy: Strategy; warnings: string[] }>("/import", { body: { text } });
      if (r.warnings.length) {
        setWarnings(r.warnings);
        window.setTimeout(() => onDone(r.strategy), 2500);
      } else await onDone(r.strategy);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div className="modal wide" role="dialog" aria-modal="true" onMouseDown={(e) => e.stopPropagation()}>
        <h3>Import a symphony</h3>
        <p className="muted small">
          Paste a Composer symphony (the EDN from its editor's source view, or its JSON), or a msts-trader strategy file. Blocks that can't be
          mapped are listed rather than silently dropped.
        </p>
        <textarea className="code-input" rows={14} value={text} onChange={(e) => setText(e.target.value)} placeholder='{:step :root :name "My symphony" :children [...]}' spellCheck={false} />
        {err && <pre className="alert error pre">{err}</pre>}
        {warnings.map((w, i) => (
          <div className="alert warn small" key={i}>
            {w}
          </div>
        ))}
        <div className="btn-row end">
          <button className="btn ghost" onClick={onClose}>
            Cancel
          </button>
          <button className="btn primary" disabled={!text.trim() || busy} onClick={go}>
            {busy ? "Importing…" : "Import"}
          </button>
        </div>
      </div>
    </div>
  );
}
