// FE-A's additions to the specialists API (the routes are in docs/specialists.md §8). The
// shared fetchers live in platform/lib/specialists-api.ts (FE-B owns it);
// what only the Specialists page needs is spelled here.
import { getJson, mutateJson } from "@platform/lib/api";
import { SPECIALISTS_API, type ChangeItem, type SpecialistSummary } from "@platform/lib/specialists-api";

const one = (id: string) => `${SPECIALISTS_API}/${encodeURIComponent(id)}`;

/** GET …/nightly: `started` is the live run's start, epoch seconds. */
export interface NightlyStatus {
  last_ok: number | null;
  running: boolean;
  last_row: Record<string, unknown> | string | null;
  started?: number | null;
  /** The last update stopped early and changed nothing ("interrupted" | "error"), else null. */
  stopped_empty?: "interrupted" | "error" | string | null;
}
export const getNightlyStatus = (id: string) => getJson<NightlyStatus>(`${one(id)}/nightly`);

/** Opening the Learned tab: every update up to `run` is seen ("Learned · N new" clears). */
export const markLearnedSeen = (id: string, run: string) =>
  mutateJson<{ seen: string; new: number }>("POST", `${one(id)}/learned/seen`, { run });

/** The quiet line for an update that stopped early and changed nothing (null: none). */
export function stoppedLine(why: string | null | undefined): string | null {
  if (why === "interrupted") return "The last update was interrupted and changed nothing. Run now to try again.";
  if (why === "error") return "The last update stopped with an error and changed nothing. Run now to try again.";
  return null;
}

/** A list row with the specialist's `model`. */
export type SpecialistRow = SpecialistSummary & { model?: string };

/** The fixed first characters of the revert request. */
export const REVERT_PREFIX = "Undo this change from the latest update: ";

/** Used only when the server's answer carries no message of its own. */
export function revertFallback(item: ChangeItem): string {
  return `${REVERT_PREFIX}"${item.text}". Change only that, and tell me in plain words what you changed.`;
}

/** Item states: Revert is offered only on an active item. */
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

/**
 * A note's claim or evidence as the user reads it. Notes are written by models
 * and may cite an entry by id ("this contradicts [m-0001]"): a known entry
 * reads as its first words ("the entry “Our tile service serves…”"), an
 * unknown id is dropped. The stored text is not changed.
 */
export function noteText(text: string, entries: readonly { id: string; text: string }[] = []): string {
  if (!text) return text;
  const known = new Map(entries.map((e) => [e.id, e.text]));
  // "(see [m-0001])" with an unknown id says nothing once the id is gone: drop the brackets too.
  const bare = text.replace(/\s*\(\s*(?:see|per|cf\.?)?\s*\[?\b(m-[0-9a-f]{4,8})\b\]?\s*\)/gi, (m, id: string) =>
    known.has(id) ? m : "",
  );
  const out = bare.replace(/\[?\b(m-[0-9a-f]{4,8})\b\]?/g, (_m, id: string) => {
    const words = known.get(id);
    return words ? `the entry “${firstWords(words)}”` : "";
  });
  return out
    .replace(/\(\s*\)/g, "")
    .replace(/\s+([.,;:!?)])/g, "$1")
    .replace(/[ \t]{2,}/g, " ")
    .replace(/,(?=[.;:!?])/g, "")
    .trim();
}

/** The first few words of an entry, cut at a word, with an ellipsis when cut. */
function firstWords(text: string, n = 6): string {
  const words = (text || "").replace(/\s+/g, " ").trim().split(" ");
  const head = words.slice(0, n).join(" ").replace(/[\s,.;:·-]+$/, "");
  return words.length > n ? `${head}…` : head;
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
/** What the one model setting means. */
export const MODEL_HINT =
  "The default for its chat, its breakouts and its background work. The chat's model pill can still change it for one chat.";
