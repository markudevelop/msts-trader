import { useState } from "react";
import { createRoot } from "react-dom/client";

// In-page replacements for window.confirm / window.prompt. Embedded browsers
// (and a tab where the user ticked "prevent additional dialogs") make the
// native confirm return false without showing anything and prompt throw, so
// Delete, Tag and "Discard unsaved changes?" silently did nothing.

type Opts = { ok?: string; danger?: boolean };

function mount<T>(render: (done: (v: T) => void) => JSX.Element): Promise<T> {
  return new Promise((resolve) => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    const done = (v: T) => {
      root.unmount();
      host.remove();
      resolve(v);
    };
    root.render(render(done));
  });
}

function Dialog({ message, input, opts, done }: { message: string; input?: string; opts: Opts; done: (v: string | null) => void }) {
  const [text, setText] = useState(input ?? "");
  const ok = () => done(input === undefined ? "" : text);
  return (
    <div className="modal-backdrop" onMouseDown={() => done(null)}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        onMouseDown={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === "Escape") done(null);
          if (e.key === "Enter") ok();
        }}
      >
        <p style={{ whiteSpace: "pre-wrap" }}>{message}</p>
        {input !== undefined && <input autoFocus value={text} onChange={(e) => setText(e.target.value)} />}
        <div className="btn-row end">
          <button className="btn ghost" onClick={() => done(null)}>
            Cancel
          </button>
          <button className={`btn ${opts.danger ? "danger" : "primary"}`} autoFocus={input === undefined} onClick={ok}>
            {opts.ok ?? "OK"}
          </button>
        </div>
      </div>
    </div>
  );
}

export const confirmDialog = (message: string, opts: Opts = {}): Promise<boolean> =>
  mount<string | null>((done) => <Dialog message={message} opts={opts} done={done} />).then((v) => v !== null);

export const promptDialog = (message: string, initial = "", opts: Opts = {}): Promise<string | null> =>
  mount<string | null>((done) => <Dialog message={message} input={initial} opts={opts} done={done} />);
