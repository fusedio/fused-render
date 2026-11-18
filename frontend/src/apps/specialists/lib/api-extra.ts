// FE-A's additions to the specialists API (fix wave CONTRACT C4, C5). The
// shared fetchers live in platform/lib/specialists-api.ts (FE-B owns it);
// what only the Specialists page needs is spelled here.
import { getJson } from "@platform/lib/api";
import { SPECIALISTS_API, type ChangeItem, type SpecialistSummary } from "@platform/lib/specialists-api";

const one = (id: string) => `${SPECIALISTS_API}/${encodeURIComponent(id)}`;

/** GET …/nightly (C5): `started` is the live run's start, epoch seconds. */
export interface NightlyStatus {
  last_ok: number | null;
  running: boolean;
  last_row: Record<string, unknown> | string | null;
  started?: number | null;
}
export const getNightlyStatus = (id: string) => getJson<NightlyStatus>(`${one(id)}/nightly`);

/** A list row with C5's `model`. */
export type SpecialistRow = SpecialistSummary & { model?: string };

/** C3: the fixed first characters of the revert request. */
export const REVERT_PREFIX = "Undo this change from the latest update: ";

/** Used only when the server's answer carries no message of its own. */
export function revertFallback(item: ChangeItem): string {
  return `${REVERT_PREFIX}"${item.text}". Change only that, and tell me in plain words what you changed.`;
}

/** C4 item states: Revert is offered only on an active item. */
export function revertable(state: string | undefined): boolean {
  return !state || state === "active";
}

/**
 * Memory and notes are written by the model, often in the third person ("in
 * the user's projects"). The reader is that user: say it to them.
 */
export function secondPerson(text: string): string {
  if (!text) return text;
  // "The user" at the start of a sentence keeps its capital: "You", "Your".
  const cap = (s: string, m: string) => (/^[A-Z]/.test(m) ? s[0].toUpperCase() + s.slice(1) : s);
  return text
    .replace(/\b(this|the) user's\b/gi, (m) => cap("your", m))
    // Only as an object ("asked by the user"): as a subject the verb would
    // no longer agree ("the user prefers" → "you prefers").
    .replace(/\b(for|to|with|by|asks|told|tell|ask) (this|the) user\b(?!')/gi, (_m, p: string) => `${p} you`);
}

/** "Q4 canvas sync stalls after…" — one line, cut at a word. */
export function clip(text: string, max = 60): string {
  const t = (text || "").replace(/\s+/g, " ").trim();
  if (t.length <= max) return t;
  const cut = t.slice(0, max);
  const sp = cut.lastIndexOf(" ");
  return (sp > max * 0.6 ? cut.slice(0, sp) : cut).replace(/[\s,.;:·-]+$/, "") + "…";
}

/** "12 s ago", "3 min ago". */
export function agoText(epochS: number, nowMs: number = Date.now()): string {
  const s = Math.max(0, Math.round(nowMs / 1000 - epochS));
  if (s < 60) return `${s} s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  return `${h} h ago`;
}

/** "Today 14:32", "Yesterday 09:10", "12 Nov 14:32". */
export function whenText(epochS: number, now: Date = new Date()): string {
  if (!epochS) return "";
  const d = new Date(epochS * 1000);
  const start = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((start(now) - start(d)) / 86_400_000);
  const time = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  if (days <= 0) return `Today ${time}`;
  if (days === 1) return `Yesterday ${time}`;
  return `${d.toLocaleDateString(undefined, { day: "numeric", month: "short" })} ${time}`;
}

/** The app's own Connections settings, where a service is signed in. */
export const CONNECTIONS_HREF = "/preferences?s=connections";

// The model and effort choices (Settings › General, the Create dialog).
export const MODELS = ["", "sonnet", "opus", "haiku"] as const;
export const EFFORTS = ["", "low", "medium", "high"] as const;
/** "Default" for "", else the word with a capital. */
export const optionLabel = (m: string) => (m ? m[0].toUpperCase() + m.slice(1) : "Default");
/** R7: what the one model setting means. */
export const MODEL_HINT =
  "The default for its chat, its breakouts and its background work. The chat's model pill can still change it for one chat.";
