// Every /api call carries the per-process session token printed by
// `msts-trader ui` (?t=TOKEN). It is moved out of the address bar into
// sessionStorage on load so it doesn't linger in history or get copied along.

const KEY = "msts-token";

function readToken(): string {
  const url = new URL(window.location.href);
  const t = url.searchParams.get("t");
  if (t) {
    try {
      sessionStorage.setItem(KEY, t);
    } catch {
      /* storage blocked: keep it in memory only */
    }
    url.searchParams.delete("t");
    window.history.replaceState(null, "", url.pathname + url.search + url.hash);
    return t;
  }
  try {
    return sessionStorage.getItem(KEY) ?? "";
  } catch {
    return "";
  }
}

const token = readToken();

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export async function api<T>(path: string, init: { method?: string; body?: unknown } = {}): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method: init.method ?? (init.body === undefined ? "GET" : "POST"),
    headers: {
      "X-MSTS-Token": token,
      ...(init.body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    body: init.body === undefined ? undefined : JSON.stringify(init.body),
  });
  const text = await res.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!res.ok) {
    const detail = (data && typeof data === "object" && "detail" in data ? (data as { detail: unknown }).detail : data) ?? res.statusText;
    throw new ApiError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data as T;
}

export const hasToken = () => token.length > 0;

/** POST and return the raw response body (e.g. a zip download). */
export async function apiBlob(path: string, body: unknown): Promise<Blob> {
  const res = await fetch(`/api${path}`, {
    method: "POST",
    headers: { "X-MSTS-Token": token, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new ApiError(res.status, (await res.text()) || res.statusText);
  return res.blob();
}

export function download(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
