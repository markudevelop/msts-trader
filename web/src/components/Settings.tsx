import { useEffect, useState } from "react";
import { api } from "../api";
import type { StudioSettings } from "../types";

/** Notification settings. Secrets (webhook URL, Telegram token) are write-only:
 *  the server keeps them in the OS keychain and only reports whether they're set. */
export function SettingsModal({ onClose }: { onClose: () => void }) {
  const [s, setS] = useState<StudioSettings | null>(null);
  const [url, setUrl] = useState("");
  const [tgToken, setTgToken] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    api<StudioSettings>("/settings").then(setS).catch((e) => setMsg({ ok: false, text: e.message }));
  }, []);

  const put = async (body: Record<string, unknown>, label: string) => {
    setBusy(label);
    setMsg(null);
    try {
      setS(await api<StudioSettings>("/settings", { method: "PUT", body }));
      setMsg({ ok: true, text: "Saved" });
      return true;
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
      return false;
    } finally {
      setBusy(null);
    }
  };

  const saveSecrets = async () => {
    const body: Record<string, unknown> = {};
    if (url.trim()) body.notify_url = url.trim();
    if (tgToken.trim()) body.telegram_token = tgToken.trim();
    if (await put(body, "secrets")) {
      setUrl("");
      setTgToken("");
    }
  };

  const test = async () => {
    setBusy("test");
    setMsg(null);
    try {
      const r = await api<{ sent: string[]; failed: string[] }>("/settings/test", { body: {} });
      setMsg({ ok: !r.failed.length, text: `Sent via ${r.sent.join(", ") || "nothing"}${r.failed.length ? ` · failed: ${r.failed.join(", ")}` : ""}` });
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div className="modal wide" role="dialog" aria-modal="true" onMouseDown={(e) => e.stopPropagation()}>
        <h3>Notifications</h3>
        {!s ? (
          <p className="muted small">Loading…</p>
        ) : (
          <>
            <p className="muted small">
              Studio messages you after strategy runs: what was bought and sold, or why a run failed. Secrets are stored in your OS keychain and never shown again.
            </p>
            <div className="form-grid">
              <label>
                Webhook URL <span className="muted small">(Discord, Slack, or any JSON webhook)</span>
                <input
                  type="password"
                  autoComplete="off"
                  value={url}
                  placeholder={s.has_notify_url ? "set ✓ (type to replace)" : "https://discord.com/api/webhooks/…"}
                  onChange={(e) => setUrl(e.target.value)}
                />
              </label>
              <label>
                Telegram bot token
                <input
                  type="password"
                  autoComplete="off"
                  value={tgToken}
                  placeholder={s.has_telegram_token ? "set ✓ (type to replace)" : "123456:ABC…"}
                  onChange={(e) => setTgToken(e.target.value)}
                />
              </label>
              <label>
                Telegram chat id
                <input value={s.telegram_chat_id} onChange={(e) => setS({ ...s, telegram_chat_id: e.target.value })} onBlur={() => put({ telegram_chat_id: s.telegram_chat_id }, "chat")} />
              </label>
              <label>
                Notify after
                <select value={s.notify_on} onChange={(e) => put({ notify_on: e.target.value }, "on")}>
                  <option value="live">live runs and errors</option>
                  <option value="all">every run, including previews</option>
                  <option value="off">never</option>
                </select>
              </label>
              <label className="check">
                <input type="checkbox" checked={s.weekly_digest} onChange={(e) => put({ weekly_digest: e.target.checked }, "digest")} />
                Weekly digest of funded strategies (capital, value, P&amp;L, last run): Fridays after the close
              </label>
            </div>
            <div className="btn-row">
              <button className="btn primary" disabled={!!busy || (!url.trim() && !tgToken.trim())} onClick={saveSecrets}>
                {busy === "secrets" ? "Saving…" : "Save secrets"}
              </button>
              <button className="btn" disabled={!!busy || !(s.has_notify_url || (s.has_telegram_token && s.telegram_chat_id))} onClick={test}>
                {busy === "test" ? "Sending…" : "Send test"}
              </button>
              {s.has_notify_url && (
                <button className="btn ghost small" disabled={!!busy} onClick={() => put({ notify_url: "" }, "clear")}>
                  Remove webhook
                </button>
              )}
              {s.has_telegram_token && (
                <button className="btn ghost small" disabled={!!busy} onClick={() => put({ telegram_token: "" }, "clear")}>
                  Remove Telegram token
                </button>
              )}
            </div>
          </>
        )}
        {msg && <div className={`alert ${msg.ok ? "ok" : "error"} small`}>{msg.text}</div>}
        <div className="btn-row end">
          <button className="btn ghost" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  );
}

/** Inline tag chips with add/remove; suggestions from the whole library. */
export function TagEditor({ tags, onChange, suggestions }: { tags: string[]; onChange: (t: string[]) => void; suggestions: string[] }) {
  const [text, setText] = useState("");
  const add = () => {
    const t = text.trim().replace(/\s+/g, " ").slice(0, 40);
    if (t && !tags.some((x) => x.toLowerCase() === t.toLowerCase())) onChange([...tags, t]);
    setText("");
  };
  return (
    <div className="tag-editor">
      {tags.map((t) => (
        <span className="tag-chip" key={t}>
          {t}
          <button aria-label={`Remove tag ${t}`} onClick={() => onChange(tags.filter((x) => x !== t))}>
            ×
          </button>
        </span>
      ))}
      <input
        className="tag-input"
        list="tag-suggestions"
        value={text}
        placeholder="+ tag"
        aria-label="Add tag"
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            add();
          }
        }}
        onBlur={() => text.trim() && add()}
      />
      <datalist id="tag-suggestions">
        {suggestions
          .filter((s) => !tags.includes(s))
          .map((s) => (
            <option key={s} value={s} />
          ))}
      </datalist>
    </div>
  );
}
