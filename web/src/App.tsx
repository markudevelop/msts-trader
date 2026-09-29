import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError, hasToken } from "./api";
import { TEMPLATES, treeTickers } from "./blocks";
import { BacktestPanel } from "./components/Backtest";
import { DeployPanel } from "./components/Deploy";
import { NodeList } from "./components/Editor";
import type { Cadence, EvalResult, FeedCatalog, Meta, Strategy, StrategySummary, UrlFeedTest } from "./types";

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
              {route.tab === "backtest" && (
                <BacktestPanel
                  draft={draft}
                  others={list.filter((x) => x.id !== draft.id)}
                  onSaved={async (s) => {
                    await loadList();
                    if (dirty && !window.confirm("Open the new blend? Unsaved changes to this strategy will be discarded.")) return;
                    setSaved(s);
                    setDraft(s);
                    window.location.hash = `/${s.id}/build`;
                  }}
                />
              )}
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
  const [tab, setTab] = useState<"pnl" | "url" | "composer">("pnl");
  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div className="modal wide" role="dialog" aria-modal="true" onMouseDown={(e) => e.stopPropagation()}>
        <h3>Import</h3>
        <div className="seg wide-seg" role="tablist">
          <button className={tab === "pnl" ? "on" : ""} onClick={() => setTab("pnl")} role="tab" aria-selected={tab === "pnl"}>
            pnlportfolio books
          </button>
          <button className={tab === "url" ? "on" : ""} onClick={() => setTab("url")} role="tab" aria-selected={tab === "url"}>
            Custom feed
          </button>
          <button className={tab === "composer" ? "on" : ""} onClick={() => setTab("composer")} role="tab" aria-selected={tab === "composer"}>
            Composer / JSON
          </button>
        </div>
        {tab === "pnl" && <PnlImport onClose={onClose} onDone={onDone} />}
        {tab === "url" && <CustomFeedImport onClose={onClose} onDone={onDone} />}
        {tab === "composer" && <ComposerImport onClose={onClose} onDone={onDone} />}
      </div>
    </div>
  );
}

function PnlImport({ onClose, onDone }: { onClose: () => void; onDone: (s: Strategy) => Promise<void> }) {
  const [cat, setCat] = useState<FeedCatalog | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [token, setToken] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    api<FeedCatalog>("/feeds")
      .then((c) => {
        setCat(c);
        setPicked(c.books.filter((b) => b.featured).map((b) => b.id));
      })
      .catch((e) => setErr(e.message));
  }, []);

  const saveToken = async () => {
    setBusy("token");
    setErr(null);
    try {
      await api("/feeds/token", { method: "PUT", body: { token } });
      setToken("");
      setCat((c) => (c ? { ...c, has_token: true } : c));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const doImport = async () => {
    setBusy("import");
    setErr(null);
    try {
      const r = await api<{ created: Strategy[] }>("/feeds/import", { body: { books: picked } });
      if (r.created.length) await onDone(r.created[0]);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const toggle = (id: string) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]));
  const shown = (cat?.books ?? []).filter((b) => b.featured || !q || `${b.id} ${b.label}`.toLowerCase().includes(q.toLowerCase()));

  return (
    <>
      <p className="muted small">
        Each book becomes a strategy holding exactly what pnlportfolio.com publishes: today's weights for live runs, and its published NAV for
        backtests. Fund, schedule and track it like any other strategy, or blend it with your own.
      </p>
      {!cat && !err && <p className="muted small">Loading books…</p>}
      {cat && (
        <div className={`token-row ${cat.has_token ? "ok" : ""}`}>
          {cat.has_token ? (
            <span className="small">✓ API token stored in your OS keychain</span>
          ) : (
            <span className="small">Live weights need your pnlportfolio API token. It is stored in your OS keychain, never in strategy files.</span>
          )}
          <input
            type="password"
            autoComplete="off"
            placeholder={cat.has_token ? "Replace token" : "API token"}
            value={token}
            onChange={(e) => setToken(e.target.value)}
          />
          <button className="btn small" disabled={!token.trim() || !!busy} onClick={saveToken}>
            {busy === "token" ? "Checking…" : "Save token"}
          </button>
        </div>
      )}
      {cat && (
        <>
          <input className="grow" placeholder={`Search all ${cat.books.length} books…`} value={q} onChange={(e) => setQ(e.target.value)} />
          <div className="book-list">
            {shown.map((b) => (
              <label key={b.id} className={`book ${picked.includes(b.id) ? "on" : ""}`}>
                <input type="checkbox" checked={picked.includes(b.id)} onChange={() => toggle(b.id)} />
                <span className="grow">
                  <b>{b.label}</b> <span className="muted small mono">{b.id}</span>
                  {b.featured && <span className="pill pill-blend">Research</span>}
                </span>
                <span className="muted small">
                  {b.num_positions ?? "–"} pos · {b.date ?? "–"}
                </span>
              </label>
            ))}
          </div>
        </>
      )}
      {err && <div className="alert error small">{err}</div>}
      <div className="btn-row end">
        <button className="btn ghost" onClick={onClose}>
          Cancel
        </button>
        <button className="btn primary" disabled={!picked.length || !!busy || !cat} onClick={doImport}>
          {busy === "import" ? "Importing…" : `Import ${picked.length} book${picked.length === 1 ? "" : "s"}`}
        </button>
      </div>
    </>
  );
}

function CustomFeedImport({ onClose, onDone }: { onClose: () => void; onDone: (s: Strategy) => Promise<void> }) {
  const [name, setName] = useState("");
  const [weightsUrl, setWeightsUrl] = useState("");
  const [navUrl, setNavUrl] = useState("");
  const [auth, setAuth] = useState<"none" | "bearer" | "query">("none");
  const [tokenParam, setTokenParam] = useState("token");
  const [token, setToken] = useState("");
  const [test, setTest] = useState<UrlFeedTest | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const body = () => ({
    weights_url: weightsUrl.trim(),
    nav_url: navUrl.trim() || null,
    auth,
    token_param: tokenParam.trim() || "token",
    token: token.trim() || null,
  });

  const runTest = async () => {
    setBusy("test");
    setErr(null);
    setTest(null);
    try {
      setTest(await api<UrlFeedTest>("/feeds/url/test", { body: body() }));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const doImport = async () => {
    setBusy("import");
    setErr(null);
    try {
      const s = await api<Strategy>("/feeds/url/import", { body: { ...body(), name: name.trim() } });
      await onDone(s);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const valid = /^https?:\/\/\S+$/.test(weightsUrl.trim()) && (!navUrl.trim() || /^https?:\/\/\S+$/.test(navUrl.trim()));

  return (
    <>
      <p className="muted small">
        Any URL that serves target weights: <code>ticker,weight</code> CSV (the format msts-trader already reads) or JSON{" "}
        <code>{'{"weights": {...}}'}</code>. For example a Google Sheet published as CSV, a raw GitHub file, or your own script.
      </p>
      <div className="form-grid">
        <label>
          Name
          <input value={name} placeholder="My model" onChange={(e) => setName(e.target.value)} />
        </label>
        <label>
          Weights URL
          <input value={weightsUrl} placeholder="https://…/weights.csv" spellCheck={false} onChange={(e) => setWeightsUrl(e.target.value)} />
        </label>
        <label>
          History URL <span className="muted small">(optional, date,nav CSV)</span>
          <input value={navUrl} placeholder="https://…/nav.csv" spellCheck={false} onChange={(e) => setNavUrl(e.target.value)} />
        </label>
        <label>
          Token
          <span className="inline">
            <select value={auth} onChange={(e) => setAuth(e.target.value as "none" | "bearer" | "query")}>
              <option value="none">none</option>
              <option value="bearer">Authorization: Bearer</option>
              <option value="query">query parameter</option>
            </select>
            {auth === "query" && <input className="num wide" value={tokenParam} onChange={(e) => setTokenParam(e.target.value)} aria-label="Query parameter name" />}
          </span>
        </label>
        {auth !== "none" && (
          <label>
            Token value <span className="muted small">(stored in your OS keychain, never in files)</span>
            <input type="password" autoComplete="off" value={token} placeholder={test?.token_stored ? "stored ✓ (leave empty)" : ""} onChange={(e) => setToken(e.target.value)} />
          </label>
        )}
      </div>
      {test && (
        <div className="test-result">
          <div>
            ✓ {test.positions} positions · gross {(test.gross * 100).toFixed(1)}%{test.asof ? ` · as of ${test.asof}` : ""}
          </div>
          <div className="chips">
            {test.top.map(([t, w]) => (
              <span className="chip" key={t}>
                {t} <b>{(w * 100).toFixed(1)}%</b>
              </span>
            ))}
          </div>
          {test.history && (
            <div>
              ✓ history {test.history.start} → {test.history.end} ({test.history.days.toLocaleString()} days)
            </div>
          )}
          {!navUrl.trim() && <div className="muted small">No history URL: backtests start once Studio has recorded a few days of this feed.</div>}
          {test.history_error && <div className="neg small">History: {test.history_error}</div>}
          {test.stops > 0 && <div className="muted small">{test.stops} protective stop(s) in the feed will not be placed (sleeves don't place stops).</div>}
        </div>
      )}
      {err && <div className="alert error small">{err}</div>}
      <div className="btn-row end">
        <button className="btn ghost" onClick={onClose}>
          Cancel
        </button>
        <button className="btn" disabled={!valid || !!busy} onClick={runTest}>
          {busy === "test" ? "Testing…" : "Test"}
        </button>
        <button className="btn primary" disabled={!valid || !name.trim() || !!busy} onClick={doImport}>
          {busy === "import" ? "Importing…" : "Import"}
        </button>
      </div>
    </>
  );
}

function ComposerImport({ onClose, onDone }: { onClose: () => void; onDone: (s: Strategy) => Promise<void> }) {
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
    <>
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
    </>
  );
}
