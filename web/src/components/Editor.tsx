import { useEffect, useRef, useState } from "react";
import { ADDABLE, COMPARATOR_LABEL, INDICATORS, INDICATOR_LABEL, STEP_HINT, STEP_LABEL, newNode } from "../blocks";
import type { Comparator, Condition, IndicatorFn, Metric, Node, Step } from "../types";

type ListProps = { nodes: Node[]; onChange: (nodes: Node[]) => void; parent: Step | "root"; depth: number };

export function NodeList({ nodes, onChange, parent, depth }: ListProps) {
  const set = (i: number, n: Node) => onChange(nodes.map((x, k) => (k === i ? n : x)));
  const remove = (i: number) => onChange(nodes.filter((_, k) => k !== i));
  const move = (i: number, d: -1 | 1) => {
    const j = i + d;
    if (j < 0 || j >= nodes.length) return;
    const next = nodes.slice();
    [next[i], next[j]] = [next[j], next[i]];
    onChange(next);
  };
  const duplicate = (i: number) => onChange([...nodes.slice(0, i + 1), structuredClone(nodes[i]), ...nodes.slice(i + 1)]);
  const add = (step: Step) => {
    const n = newNode(step);
    if (parent === "wt-cash-specified") n.weight = nodes.length ? 0 : 1;
    onChange([...nodes, n]);
  };

  return (
    <div className="node-list">
      {nodes.map((n, i) => (
        <NodeCard
          key={i}
          node={n}
          parent={parent}
          depth={depth}
          onChange={(x) => set(i, x)}
          onRemove={() => remove(i)}
          onUp={i > 0 ? () => move(i, -1) : undefined}
          onDown={i < nodes.length - 1 ? () => move(i, 1) : undefined}
          onDuplicate={() => duplicate(i)}
        />
      ))}
      <AddMenu onAdd={add} empty={nodes.length === 0} />
    </div>
  );
}

function AddMenu({ onAdd, empty }: { onAdd: (s: Step) => void; empty: boolean }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as globalThis.Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);
  return (
    <div className="add-menu" ref={ref}>
      <button className={`add-btn ${empty ? "empty" : ""}`} onClick={() => setOpen(!open)}>
        + {empty ? "Add a block (empty = cash)" : "Add block"}
      </button>
      {open && (
        <div className="menu" role="menu">
          {ADDABLE.map((s) => (
            <button
              key={s}
              role="menuitem"
              className="menu-item"
              onClick={() => {
                onAdd(s);
                setOpen(false);
              }}
            >
              <span className={`tag tag-${tagKind(s)}`}>{STEP_LABEL[s]}</span>
              <span className="muted">{STEP_HINT[s]}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function tagKind(s: Step) {
  if (s === "asset") return "asset";
  if (s === "if") return "if";
  if (s === "filter") return "filter";
  if (s === "group") return "group";
  return "weight";
}

type CardProps = {
  node: Node;
  parent: Step | "root";
  depth: number;
  onChange: (n: Node) => void;
  onRemove: () => void;
  onUp?: () => void;
  onDown?: () => void;
  onDuplicate: () => void;
};

function NodeCard({ node, parent, depth, onChange, onRemove, onUp, onDown, onDuplicate }: CardProps) {
  const [collapsed, setCollapsed] = useState(false);
  const kind = tagKind(node.step);
  const weightInput =
    parent === "wt-cash-specified" ? (
      <label className="weight-input" title="Share of the parent's allocation">
        <NumInput value={round((node.weight ?? 0) * 100, 4)} onChange={(v) => onChange({ ...node, weight: v / 100 })} min={0} max={300} step={1} />%
      </label>
    ) : null;
  const actions = (
    <div className="card-actions">
      {onUp && <IconBtn label="Move up" onClick={onUp}>↑</IconBtn>}
      {onDown && <IconBtn label="Move down" onClick={onDown}>↓</IconBtn>}
      <IconBtn label="Duplicate" onClick={onDuplicate}>⧉</IconBtn>
      <IconBtn label="Delete" onClick={onRemove} danger>✕</IconBtn>
    </div>
  );

  if (node.step === "asset") {
    return (
      <div className="card card-asset">
        {weightInput}
        <span className="tag tag-asset">Asset</span>
        <TickerInput value={node.ticker} onChange={(t) => onChange({ ...node, ticker: t })} />
        {node.name && <span className="muted small">{node.name}</span>}
        <span className="spacer" />
        {actions}
      </div>
    );
  }

  const hasKids = node.step === "if" ? node.then.length + node.else.length : node.children.length;
  return (
    <div className={`card card-${kind}`} style={{ ["--depth" as string]: depth }}>
      <div className="card-head">
        <button className="chev" aria-label={collapsed ? "Expand" : "Collapse"} onClick={() => setCollapsed(!collapsed)}>
          {collapsed ? "▸" : "▾"}
        </button>
        {weightInput}
        <span className={`tag tag-${kind}`}>{STEP_LABEL[node.step]}</span>
        <Params node={node} onChange={onChange} />
        <span className="spacer" />
        {collapsed && <span className="muted small">{hasKids} block{hasKids === 1 ? "" : "s"}</span>}
        {actions}
      </div>
      {!collapsed && <Body node={node} onChange={onChange} depth={depth} />}
    </div>
  );
}

function Params({ node, onChange }: { node: Node; onChange: (n: Node) => void }) {
  switch (node.step) {
    case "group":
      return <input className="inline-text" value={node.name} onChange={(e) => onChange({ ...node, name: e.target.value })} aria-label="Group name" />;
    case "wt-inverse-vol":
      return (
        <span className="params">
          over <NumInput value={node.window} onChange={(v) => onChange({ ...node, window: Math.max(2, Math.round(v)) })} min={2} /> days
        </span>
      );
    case "wt-cash-specified": {
      const sum = node.children.reduce((a, c) => a + (c.weight ?? 0), 0);
      const ok = Math.abs(sum - 1) < 1e-6;
      return (
        <span className={`params sum ${ok ? "ok" : "warn"}`} title={ok ? "" : "Weights under 100% leave cash; over 100% is leverage"}>
          Σ {round(sum * 100, 2)}%
        </span>
      );
    }
    case "filter":
      return (
        <span className="params">
          <select value={node.select} onChange={(e) => onChange({ ...node, select: e.target.value as "top" | "bottom" })} aria-label="Top or bottom">
            <option value="top">Top</option>
            <option value="bottom">Bottom</option>
          </select>
          <NumInput value={node.n} onChange={(v) => onChange({ ...node, n: Math.max(1, Math.round(v)) })} min={1} />
          by
          <FnSelect value={node.sort_fn} onChange={(fn) => onChange({ ...node, sort_fn: fn })} />
          {node.sort_fn !== "current-price" && (
            <>
              <NumInput value={node.window} onChange={(v) => onChange({ ...node, window: Math.max(1, Math.round(v)) })} min={1} />d
            </>
          )}
        </span>
      );
    default:
      return null;
  }
}

function Body({ node, onChange, depth }: { node: Node; onChange: (n: Node) => void; depth: number }) {
  if (node.step === "asset") return null;
  if (node.step === "if") {
    return (
      <div className="card-body">
        <ConditionEditor value={node.condition} onChange={(c) => onChange({ ...node, condition: c })} />
        <div className="branch">
          <div className="branch-label then">then</div>
          <NodeList nodes={node.then} onChange={(then) => onChange({ ...node, then })} parent="if" depth={depth + 1} />
        </div>
        <div className="branch">
          <div className="branch-label else">else</div>
          <NodeList nodes={node.else} onChange={(els) => onChange({ ...node, else: els })} parent="if" depth={depth + 1} />
        </div>
      </div>
    );
  }
  return (
    <div className="card-body">
      <NodeList nodes={node.children} onChange={(children) => onChange({ ...node, children } as Node)} parent={node.step} depth={depth + 1} />
    </div>
  );
}

function ConditionEditor({ value, onChange }: { value: Condition; onChange: (c: Condition) => void }) {
  const fixed = value.rhs === null;
  return (
    <div className="condition">
      <span className="kw">if</span>
      <MetricEditor value={value.lhs} onChange={(lhs) => onChange({ ...value, lhs })} />
      <select className="cmp" value={value.comparator} onChange={(e) => onChange({ ...value, comparator: e.target.value as Comparator })} aria-label="Comparator">
        {(Object.keys(COMPARATOR_LABEL) as Comparator[]).map((c) => (
          <option key={c} value={c}>
            {COMPARATOR_LABEL[c]}
          </option>
        ))}
      </select>
      <div className="seg" role="group" aria-label="Compare against">
        <button className={fixed ? "on" : ""} onClick={() => onChange({ ...value, rhs: null, rhs_value: value.rhs_value ?? 0 })}>
          value
        </button>
        <button
          className={!fixed ? "on" : ""}
          onClick={() => onChange({ ...value, rhs: value.rhs ?? { ...value.lhs }, rhs_value: null })}
        >
          indicator
        </button>
      </div>
      {fixed ? (
        <NumInput value={value.rhs_value ?? 0} onChange={(v) => onChange({ ...value, rhs_value: v })} step={0.1} wide />
      ) : (
        <MetricEditor value={value.rhs!} onChange={(rhs) => onChange({ ...value, rhs })} />
      )}
    </div>
  );
}

function MetricEditor({ value, onChange }: { value: Metric; onChange: (m: Metric) => void }) {
  return (
    <span className="metric">
      <FnSelect value={value.fn} onChange={(fn) => onChange({ ...value, fn })} />
      <span className="muted">of</span>
      <TickerInput value={value.ticker} onChange={(ticker) => onChange({ ...value, ticker })} />
      {value.fn !== "current-price" && (
        <>
          <NumInput value={value.window} onChange={(v) => onChange({ ...value, window: Math.max(1, Math.round(v)) })} min={1} />
          <span className="muted">d</span>
        </>
      )}
    </span>
  );
}

function FnSelect({ value, onChange }: { value: IndicatorFn; onChange: (f: IndicatorFn) => void }) {
  return (
    <select value={value} onChange={(e) => onChange(e.target.value as IndicatorFn)} aria-label="Indicator">
      {INDICATORS.map((f) => (
        <option key={f} value={f}>
          {INDICATOR_LABEL[f]}
        </option>
      ))}
    </select>
  );
}

export function TickerInput({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <input
      className="ticker"
      value={value}
      spellCheck={false}
      aria-label="Ticker"
      size={Math.max(4, value.length + 1)}
      onChange={(e) => onChange(e.target.value.toUpperCase().replace(/[^A-Z0-9.\-^=/]/g, "").slice(0, 20))}
    />
  );
}

export function NumInput({
  value,
  onChange,
  min,
  max,
  step = 1,
  wide = false,
}: {
  value: number;
  onChange: (v: number) => void;
  min?: number;
  max?: number;
  step?: number;
  wide?: boolean;
}) {
  // Local text state so users can type "-", "0." etc. without the value snapping.
  const [text, setText] = useState(String(value));
  useEffect(() => {
    if (Number(text) !== value) setText(String(value));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);
  return (
    <input
      className={`num ${wide ? "wide" : ""}`}
      inputMode="decimal"
      value={text}
      onChange={(e) => {
        setText(e.target.value);
        const v = Number(e.target.value);
        if (e.target.value.trim() !== "" && Number.isFinite(v)) {
          onChange(Math.min(max ?? Infinity, Math.max(min ?? -Infinity, v)));
        }
      }}
      onBlur={() => setText(String(value))}
      step={step}
    />
  );
}

function IconBtn({ label, onClick, children, danger }: { label: string; onClick: () => void; children: React.ReactNode; danger?: boolean }) {
  return (
    <button className={`icon-btn ${danger ? "danger" : ""}`} onClick={onClick} title={label} aria-label={label}>
      {children}
    </button>
  );
}

function round(v: number, dp: number) {
  const k = 10 ** dp;
  return Math.round(v * k) / k;
}
