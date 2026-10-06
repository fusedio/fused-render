// Formatters, ported verbatim from OpenBot core.js / chat.js. Timestamps are
// epoch SECONDS (the wire unit); `now` is injectable for tests.

/** HTML-escape (for the few strings that still become markup: md(), tooltips built as strings). */
export const esc = (s: unknown): string =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] as string);

/** "10:38 PM" (locale hour:minute). */
export const fmtTime = (ts: number): string =>
  new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

/** Session divider text: "Today 10:38 PM", "Yesterday 9:02 AM", else "Mon, Sep 8 · 3:00 PM". */
export function fmtDay(ts: number, nowMs: number = Date.now()): string {
  const d = new Date(ts * 1000), now = new Date(nowMs), y = new Date(now);
  y.setDate(now.getDate() - 1);
  const t = fmtTime(ts);
  if (d.toDateString() === now.toDateString()) return `Today ${t}`;
  if (d.toDateString() === y.toDateString()) return `Yesterday ${t}`;
  return d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric", ...(d.getFullYear() !== now.getFullYear() ? { year: "numeric" } : {}) }) + ` · ${t}`;
}

/** Today: "10:38 PM"; otherwise "Sep 8". Empty for a falsy ts. */
export const fmtWhenShort = (ts: number | null | undefined, nowMs: number = Date.now()): string => {
  if (!ts) return "";
  const d = new Date(ts * 1000), now = new Date(nowMs);
  return d.toDateString() === now.toDateString()
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString([], { month: "short", day: "numeric" });
};

/** Relative age for the bot list: "now", "10 min ago", "3 h ago", "2 d ago"; older than a week falls back to the date. */
export const fmtAgo = (ts: number | null | undefined, nowMs: number = Date.now()): string => {
  if (!ts) return "";
  const s = Math.max(0, nowMs / 1000 - ts);
  if (s < 60) return "now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 7 * 86400) return `${Math.floor(s / 86400)} d ago`;
  return fmtWhenShort(ts, nowMs);
};

/** Full tooltip time: "Mon, Sep 8, 3:00 PM"; "—" for a falsy ts. */
export const fmtWhen = (ts: number | null | undefined): string =>
  ts ? new Date(ts * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit", month: "short", day: "numeric" }) : "—";

/** "512 B", "12 KB", "3.4 MB". */
export const fmtBytes = (n: number): string =>
  n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(0) + " KB" : (n / 1048576).toFixed(1) + " MB";

/** Seconds as "m:ss" (the dictation timer). */
export const fmtSecs = (n: number): string => `${Math.floor(n / 60)}:${String(n % 60).padStart(2, "0")}`;
