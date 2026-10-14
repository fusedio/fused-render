// Pure helpers for the phone's bot screens: routing, status words, unread,
// thread grouping, time, image scaling and the markdown-lite renderer. No
// React, no DOM — lib.test.ts runs them under `bun test`.
import type { BotEvent, BotStatus, PhoneBot } from "./types";

// ---------------------------------------------------------------- routes ----
export type Route = { name: "grid" } | { name: "bots" } | { name: "bot"; id: string };

/** The lan entry's three pages. lan.py serves lan.html for `/`, `/bots` and
 *  `/bots/<id>` (an id never holds `/`); anything else falls back to the grid. */
export function parseRoute(pathname: string): Route {
  const p = (pathname || "/").replace(/\/+$/, "") || "/";
  if (p === "/bots") return { name: "bots" };
  const m = /^\/bots\/([A-Za-z0-9_-]+)$/.exec(p);
  if (m) return { name: "bot", id: m[1] };
  return { name: "grid" };
}

export function routePath(r: Route): string {
  return r.name === "grid" ? "/" : r.name === "bots" ? "/bots" : `/bots/${encodeURIComponent(r.id)}`;
}

// ---------------------------------------------------------------- status ----
export type Tone = "idle" | "busy" | "needs" | "paused" | "error";

/** What the list row and the chat strip say about a bot, and its colour family. */
export function statusOf(b: Pick<PhoneBot, "status" | "step" | "waitingOn" | "control">): { word: string; tone: Tone } {
  const s: BotStatus = b.status;
  if (s === "waiting") return { word: "Needs you", tone: "needs" };
  if (s === "running") return { word: b.step > 0 ? `Working · step ${b.step}` : "Working", tone: "busy" };
  if (s === "paused") return { word: "Paused", tone: "paused" };
  if (s === "error") return { word: "Error", tone: "error" };
  return { word: "Idle", tone: "idle" };
}

/** The dot colour per tone (tokens.css; both themes define each). */
export const TONE_COLOR: Record<Tone, string> = {
  idle: "var(--fg-faint, var(--fg-muted))",
  busy: "var(--success)",
  needs: "var(--warning)",
  paused: "var(--fg-muted)",
  error: "var(--error)",
};

export const isActive = (status: BotStatus): boolean => status === "running" || status === "waiting";

// ---------------------------------------------------------------- unread ----
/** localStorage key of the last seq the phone showed for a bot: per host (one
 *  phone pairs with several computers) and per bot. */
export const seenKey = (host: string, id: string): string => `fused-render:bot-seen:${host}:${id}`;

/** Roles that count as "something to read". Steps and harness lines do not. */
const READABLE = new Set(["done", "question", "approval", "error"]);

/** The list row's badge: the bot said something after the seq this phone last
 *  showed. A bot never opened here (`seen` null) has nothing unread — its whole
 *  history is not news. */
export function listUnread(b: Pick<PhoneBot, "last" | "status">, seen: number | null): boolean {
  if (seen == null) return false;
  if (b.status === "waiting" && b.last && b.last.seq > seen) return true;
  return !!b.last && b.last.seq > seen && READABLE.has(b.last.role);
}

/** In a loaded thread: the readable events after `seen`. */
export function unreadCount(events: BotEvent[], seen: number | null): number {
  if (seen == null) return 0;
  let n = 0;
  for (const e of events) if (e.seq > seen && READABLE.has(e.role)) n++;
  return n;
}

// ---------------------------------------------------------------- thread ----
/** Merge a poll's events into the loaded ones: unique by seq, ascending.
 *  Already-sorted appends (the usual case) keep the old array's prefix. */
export function mergeEvents(old: BotEvent[], incoming: BotEvent[]): BotEvent[] {
  if (!incoming.length) return old;
  const have = new Set(old.map((e) => e.seq));
  const fresh = incoming.filter((e) => !have.has(e.seq));
  if (!fresh.length) return old;
  fresh.sort((a, b) => a.seq - b.seq);
  const last = old.length ? old[old.length - 1].seq : -Infinity;
  if (fresh[0].seq > last) return old.concat(fresh);
  return old.concat(fresh).sort((a, b) => a.seq - b.seq);
}

export const isStep = (e: BotEvent): boolean => e.role === "thought" || e.role === "action";

export type Item =
  | { kind: "event"; key: string; e: BotEvent; deliveries: BotEvent[] }
  | { kind: "steps"; key: string; events: BotEvent[] };

/** Rows for the thread: consecutive thought/action events fold into one
 *  "steps" item (keyed by its first seq, so the key holds while the run
 *  grows); a delivery row joins the event it records (`ref`) and renders
 *  nothing of its own. */
export function groupRows(events: BotEvent[]): Item[] {
  const deliveries = new Map<number, BotEvent[]>();
  for (const e of events) {
    if (e.role === "delivery" && e.ref != null) {
      const list = deliveries.get(e.ref);
      if (list) list.push(e);
      else deliveries.set(e.ref, [e]);
    }
  }
  const out: Item[] = [];
  for (const e of events) {
    if (e.role === "delivery" && e.ref != null) continue;
    if (isStep(e)) {
      const last = out[out.length - 1];
      if (last && last.kind === "steps") last.events.push(e);
      else out.push({ kind: "steps", key: `s${e.seq}`, events: [e] });
      continue;
    }
    out.push({ kind: "event", key: `e${e.seq}`, e, deliveries: deliveries.get(e.seq) || [] });
  }
  return out;
}

/** The first user line after `seq` — how a card was answered. */
export function answerAfter(events: BotEvent[], seq: number): BotEvent | null {
  // A line the server wrote and then did not apply (bot.py ignored_seq) never settled anything.
  const ignored = new Set(events.flatMap((e) => (e.ignored_seq != null ? [e.ignored_seq] : [])));
  for (const e of events) if (e.seq > seq && e.role === "user" && !ignored.has(e.seq)) return e;
  return null;
}

// agent_engine YES / NO, the words the gate takes as a verdict.
const YES = /^\s*(y|yes|yep|yeah|ok|okay|sure|approve|approved|go(?!\s+(to|back|on|and)\b)|go ahead|do it|proceed|confirm|allow)\b/i;
const NO = /^\s*(n|no|nope|deny|denied|stop|don'?t|cancel|skip)\b/i;

/** A settled approval card's caption, from the user line that answered it. */
export function approvalVerdict(answer: string | null | undefined): "Approved" | "Denied" | null {
  if (answer == null) return null;
  if (YES.test(answer)) return "Approved";
  if (NO.test(answer)) return "Denied";
  return null;
}

/** A multi-select question's answer: the ticked options in their own order,
 *  "A, B"; "None" when nothing is ticked (the desktop's MultiQuestion). */
export function multiAnswer(options: string[], picked: ReadonlySet<string>): string {
  return options.filter((x) => picked.has(x)).join(", ") || "None";
}

/** The options a settled multi-select answer names. Whole options are found
 *  in the answer, longest first, each bounded by the start, the end, a comma
 *  or "and", so an option that itself holds a comma or "and" stays one tick. */
export function multiPicked(options: string[], answer: string): Set<string> {
  let rest = answer.toLowerCase();
  const out = new Set<string>();
  const esc = (x: string) => x.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  for (const o of [...options].sort((a, b) => b.length - a.length)) {
    const k = optionKey(o);
    if (!k) continue;
    const re = new RegExp(`(^|,|\\band\\b)\\s*${esc(k)}\\s*(?=,|\\band\\b|$)`);
    const m = re.exec(rest);
    if (!m) continue;
    out.add(k);
    rest = rest.slice(0, m.index) + m[1] + " \u0000 " + rest.slice(m.index + m[0].length);
  }
  return out;
}

export const optionKey = (s: string | null | undefined): string => String(s ?? "").trim().toLowerCase();

/** A one-line quote of an event, for the reply strip. */
export const oneLine = (s: string, max = 140): string => {
  const t = (s || "").replace(/\*\*|`/g, "").replace(/\s+/g, " ").trim();
  return t.length > max ? t.slice(0, max - 1) + "…" : t;
};

// ---------------------------------------------------------------- time ----
export function timeAgo(ts: number, now: number): string {
  if (!ts) return "";
  const s = Math.max(0, now - ts);
  if (s < 60) return "now";
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  if (s < 7 * 86400) return `${Math.floor(s / 86400)}d`;
  return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function clock(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

/** A day divider between two events on different calendar days. */
export function dayBreak(prev: BotEvent | undefined, e: BotEvent): boolean {
  if (!prev) return true;
  return new Date(prev.ts * 1000).toDateString() !== new Date(e.ts * 1000).toDateString();
}

export function dayLabel(ts: number, now: number): string {
  const d = new Date(ts * 1000), today = new Date(now * 1000);
  if (d.toDateString() === today.toDateString()) return "Today";
  const y = new Date(today);
  y.setDate(today.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return "Yesterday";
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

// ---------------------------------------------------------------- images ----
/** The model's long-edge ceiling for an image block (runtime.js BOTS_IMAGE_EDGE). */
export const IMAGE_EDGE = 1568;
/** Attachments are limited to 8 MB each (runtime.js BOTS_FILE_MAX). */
export const FILE_MAX = 8 * 1024 * 1024;
export const IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);

/** The preview's size: never upscaled, long edge <= `edge`, at least 1 px. */
export function scaledSize(w: number, h: number, edge = IMAGE_EDGE): { w: number; h: number } {
  const W = w || 1, H = h || 1;
  const scale = Math.min(1, edge / Math.max(W, H));
  return { w: Math.max(1, Math.round(W * scale)), h: Math.max(1, Math.round(H * scale)) };
}

/** PNG stays PNG (screenshots with text); everything else goes out as JPEG. */
export const previewType = (type: string): "image/png" | "image/jpeg" =>
  type.toLowerCase() === "image/png" ? "image/png" : "image/jpeg";

// ---------------------------------------------------------------- markdown-lite ----
export function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

const LINK = /\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<]+)/g;

function anchor(href: string, label: string): string {
  return `<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>`;
}

/** Bold and links on one stretch of already-escaped text. */
function inline(escaped: string): string {
  const bolded = escaped.replace(/\*\*(?=\S)([^*\n]+?)\*\*/g, "<strong>$1</strong>");
  return bolded.replace(LINK, (_m, label: string | undefined, href: string | undefined, bare: string | undefined) => {
    if (label && href) return anchor(href, label);
    let url = bare || "";
    let tail = "";
    // Sentence punctuation after a URL is not part of it; a closing paren is
    // only when the URL opened none.
    for (;;) {
      const m = /([.,;:!?]|&#39;|&quot;|\))$/.exec(url);
      if (!m) break;
      if (m[1] === ")" && url.includes("(")) break;
      url = url.slice(0, -m[1].length);
      tail = m[1] + tail;
    }
    return anchor(url, url) + tail;
  });
}

/** The phone's renderer for a bot's text: paragraphs on blank lines, line
 *  breaks, `**bold**`, `inline code`, [label](url) and bare http(s) links
 *  (new tab). Headings and list markers stay as plain lines. Everything is
 *  escaped first; the only tags out are p, br, strong, code and a. */
export function markdownLite(src: string): string {
  const text = (src || "").replace(/\r\n?/g, "\n").trim();
  if (!text) return "";
  const paras = text.split(/\n{2,}/);
  return paras
    .map((p) => {
      const lines = p.split("\n").map((line) => {
        let l = line.replace(/^\s{0,3}#{1,6}\s+(.*)$/, "**$1**").replace(/^(\s*)[-*]\s+/, "$1• ");
        // inline code is split out first so nothing inside it is formatted
        const parts = l.split(/(`[^`\n]+`)/);
        return parts
          .map((part, i) => (i % 2 ? `<code>${escapeHtml(part.slice(1, -1))}</code>` : inline(escapeHtml(part))))
          .join("");
      });
      return `<p>${lines.join("<br>")}</p>`;
    })
    .join("");
}

// ---------------------------------------------------------------- routines ----
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/** A routine's schedule in words: "Every 2 h", "Daily at 09:00",
 *  "Weekdays at 08:30", "Mon, Thu at 18:00", "Once · Oct 14, 9:00 AM". */
export function routineWhen(r: { kind: string; minutes?: number; time?: string; weekdays?: number[]; at?: number }): string {
  if (r.kind === "interval") {
    const m = r.minutes || 60;
    if (m % 1440 === 0) return m === 1440 ? "Every day" : `Every ${m / 1440} days`;
    if (m % 60 === 0) return m === 60 ? "Every hour" : `Every ${m / 60} h`;
    return `Every ${m} min`;
  }
  if (r.kind === "daily") {
    const days = [...new Set(r.weekdays ?? [0, 1, 2, 3, 4, 5, 6])].sort((a, b) => a - b);
    const t = r.time || "09:00";
    const key = days.join("");
    const which = key === "0123456" ? "Daily" : key === "01234" ? "Weekdays" : key === "56" ? "Weekends" : days.map((d) => DAYS[d] || "?").join(", ");
    return `${which} at ${t}`;
  }
  if (r.kind === "once") {
    if (!r.at) return "Once";
    const d = new Date(r.at * 1000);
    return `Once · ${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })}, ${d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}`;
  }
  return r.kind;
}

/** `<input type=datetime-local>`'s value (local time) as epoch seconds; NaN when empty. */
export function localInputToEpoch(v: string): number {
  if (!v) return NaN;
  const t = new Date(v).getTime();
  return Number.isFinite(t) ? Math.floor(t / 1000) : NaN;
}
