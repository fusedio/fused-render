// Pure helpers for the phone's task screens: lanes and their order, the chip
// filters, search, the row's title rule, status words, time words and the
// thread's merge-by-index. No React, no DOM — lib.test.ts runs them under
// `bun test`. Time and markdown come from the bots' lib (one copy).
import { timeAgo, type Tone } from "../bots/lib";
import type { PhoneCard, PhoneTask, PhoneTurn, Project } from "./types";

export { clock, dayLabel, markdownLite, oneLine, timeAgo } from "../bots/lib";

// ---------------------------------------------------------------- lanes ----
/** The sections of the list, in the order they are drawn. */
export type Lane = "needs" | "running" | "queued" | "upcoming" | "done" | "archived";
export const LANES: Lane[] = ["needs", "running", "queued", "upcoming", "done", "archived"];
export const LANE_LABEL: Record<Lane, string> = {
  needs: "Needs you",
  running: "Running",
  queued: "Queued",
  upcoming: "Upcoming",
  done: "Done",
  archived: "Archived",
};

export function laneOf(t: Pick<PhoneTask, "status">): Lane {
  switch (t.status) {
    case "needs_attention":
    case "blocked":
      return "needs";
    case "in_progress":
      return "running";
    case "queued":
      return "queued";
    case "upcoming":
      return "upcoming";
    case "archived":
      return "archived";
    default:
      return "done";
  }
}

/** The chip strip: one of these, or null for everything. */
export type Chip = "needs" | "running" | "upcoming" | "done" | "archived";
export const CHIPS: { key: Chip; label: string }[] = [
  { key: "needs", label: "Needs you" },
  { key: "running", label: "Running" },
  { key: "upcoming", label: "Upcoming" },
  { key: "done", label: "Done" },
  { key: "archived", label: "Archived" },
];

/** Which lanes a chip shows. "Running" is the work in flight AND the line behind it. */
export function chipLanes(chip: Chip | null): Lane[] {
  switch (chip) {
    case "needs":
      return ["needs"];
    case "running":
      return ["running", "queued"];
    case "upcoming":
      return ["upcoming"];
    case "done":
      return ["done"];
    case "archived":
      return ["archived"];
    default:
      return ["needs", "running", "queued", "upcoming", "done"];
  }
}

export interface Filter {
  chip: Chip | null;
  /** Tilde'd project paths; empty = every folder. */
  folders: string[];
}

/** `localStorage` key of the list's filter, per host (one phone, several computers). */
export const filterKey = (host: string): string => `fused-render:lan-tasks-filter:${host}`;

/** The chip and folders to keep between visits: Archived is never kept on
 *  (the list opens on what is current, not on what was filed). */
export function persistedFilter(f: Filter): Filter {
  return { chip: f.chip === "archived" ? null : f.chip, folders: f.folders };
}

export function parseFilter(raw: string | null): Filter {
  if (!raw) return { chip: null, folders: [] };
  try {
    const v = JSON.parse(raw) as Partial<Filter>;
    const chip = CHIPS.some((c) => c.key === v.chip) ? (v.chip as Chip) : null;
    const folders = Array.isArray(v.folders) ? v.folders.filter((x): x is string => typeof x === "string") : [];
    return persistedFilter({ chip, folders });
  } catch {
    return { chip: null, folders: [] };
  }
}

// ---------------------------------------------------------------- search ----
function norm(s: string): string {
  return (s || "").toLowerCase();
}

/** Title, last reply, folder and task id, case-insensitively, every word matching somewhere. */
export function matches(t: PhoneTask, query: string): boolean {
  const words = norm(query).split(/\s+/).filter(Boolean);
  if (!words.length) return true;
  const hay = [t.title, t.last_reply, t.last_message?.text || "", t.project_label, t.project, t.task_id, t.repo]
    .map(norm)
    .join("\n");
  return words.every((w) => hay.includes(w));
}

/** The rows to draw, grouped by lane in LANES order: the chip's lanes, the
 *  folders picked, the search; the server's order inside a lane (newest
 *  activity first), except Upcoming, which runs soonest first. */
export function group(tasks: PhoneTask[], f: Filter, query: string): { lane: Lane; rows: PhoneTask[] }[] {
  const lanes = new Set(chipLanes(f.chip));
  const folders = new Set(f.folders);
  const by = new Map<Lane, PhoneTask[]>();
  for (const t of tasks) {
    const lane = laneOf(t);
    if (!lanes.has(lane)) continue;
    if (folders.size && !folders.has(t.project)) continue;
    if (!matches(t, query)) continue;
    const arr = by.get(lane);
    if (arr) arr.push(t);
    else by.set(lane, [t]);
  }
  const out: { lane: Lane; rows: PhoneTask[] }[] = [];
  for (const lane of LANES) {
    const rows = by.get(lane);
    if (!rows) continue;
    if (lane === "upcoming") rows.sort((a, b) => (a.next_run ?? Infinity) - (b.next_run ?? Infinity));
    out.push({ lane, rows });
  }
  return out;
}

/** "2 running, 1 queued" for the Running section head; a plain count elsewhere. */
export function laneCaption(lane: Lane, rows: PhoneTask[]): string {
  if (lane === "running") {
    const running = rows.filter((r) => r.status === "in_progress").length;
    const queued = rows.length - running;
    return queued ? `${running} running, ${queued} queued` : `${running} running`;
  }
  return String(rows.length);
}

/** Folders for the picker: by task count, then name. */
export function projectOptions(projects: Project[]): Project[] {
  return [...projects].sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
}

// ---------------------------------------------------------------- rows ----
/** The row's title line: the newest user message's first line, else the title
 *  (the desktop's `cardTitleLine` rule), one line. */
export function titleLine(t: Pick<PhoneTask, "title" | "last_message">): string {
  const m = t.last_message;
  if (m && m.role === "user" && m.text.trim()) return firstLine(m.text);
  return firstLine(t.title) || "Untitled";
}

export function firstLine(s: string): string {
  return (s || "").split(/\r?\n/).find((l) => l.trim())?.trim() || "";
}

/** The status word and its colour family, for the dot. */
export function statusOf(t: Pick<PhoneTask, "status" | "failed" | "blocked_reason" | "live">): { word: string; tone: Tone } {
  switch (t.status) {
    case "needs_attention":
      return { word: "Needs you", tone: "needs" };
    case "blocked":
      return {
        word: t.blocked_reason === "usage_limit" ? "Usage limit" : t.blocked_reason === "failed" ? "Failed" : "Blocked",
        tone: t.blocked_reason === "failed" ? "error" : "needs",
      };
    case "in_progress":
      return { word: "Working", tone: "busy" };
    case "queued":
      return { word: "Queued", tone: "paused" };
    case "upcoming":
      return { word: "Upcoming", tone: "paused" };
    case "archived":
      return { word: "Archived", tone: "idle" };
    default:
      return t.failed ? { word: "Failed", tone: "error" } : { word: "Done", tone: "idle" };
  }
}

/** "Working", "Needs you" … the task view's strip word, same table. */
export const isActive = (t: Pick<PhoneTask, "status">): boolean =>
  t.status === "in_progress" || t.status === "needs_attention" || t.status === "queued";

/** "2nd in line behind Fix the build" */
export function queueLine(t: Pick<PhoneTask, "queue_position" | "queue_ahead_title">): string {
  const n = t.queue_position;
  if (!n) return "";
  const ord = n === 1 ? "1st" : n === 2 ? "2nd" : n === 3 ? "3rd" : `${n}th`;
  return t.queue_ahead_title ? `${ord} in line behind ${t.queue_ahead_title}` : `${ord} in line`;
}

/** The agent's name for the caption: "Claude", "Codex", … */
export function backendLabel(b: string): string {
  const k = (b || "").toLowerCase();
  return k === "claude" ? "Claude" : k === "codex" ? "Codex" : k === "copilot" ? "Copilot" : k === "gemini" ? "Gemini" : b || "";
}

/** The caption under a row: folder · agent · when. */
export function caption(t: PhoneTask, now: number): string {
  const when = t.happened_at || t.last_active || t.started;
  const parts = [t.project_label, backendLabel(t.backend), when ? timeAgo(when, now) : ""].filter(Boolean);
  return parts.join(" · ");
}

/** "in 30s", "in 5 min", "in 3 h", "in 2 days"; "now" once due. */
export function inWords(at: number, now: number): string {
  const d = Math.round(at - now);
  if (d <= 0) return "now";
  if (d < 60) return `in ${d}s`;
  if (d < 3600) return `in ${Math.round(d / 60)} min`;
  if (d < 86400) return `in ${Math.round(d / 3600)} h`;
  const days = Math.round(d / 86400);
  return `in ${days} day${days === 1 ? "" : "s"}`;
}

/** A due stamp (ISO or epoch) as epoch seconds, NaN when unreadable. */
export function dueEpoch(due: string | number | null | undefined): number {
  if (due == null) return NaN;
  if (typeof due === "number") return due > 1e12 ? due / 1000 : due;
  const t = Date.parse(due);
  return Number.isFinite(t) ? t / 1000 : NaN;
}

// ---------------------------------------------------------------- thread ----
/** Turns merged by index: `incoming` replaces what it names (a re-sent last
 *  turn grown in place) and appends the rest; with `reset`, `incoming` is the
 *  whole thread. Always sorted by `i`, never a gap filled with a stale row. */
export function mergeTurns(old: PhoneTurn[], incoming: PhoneTurn[], reset = false): PhoneTurn[] {
  if (reset) return [...incoming].sort((a, b) => a.i - b.i);
  if (!incoming.length) return old;
  const by = new Map<number, PhoneTurn>();
  for (const t of old) by.set(t.i, t);
  for (const t of incoming) by.set(t.i, t);
  return [...by.values()].sort((a, b) => a.i - b.i);
}

/** A day divider goes above `cur` when its day differs from `prev`'s. */
export function dayBreak(prev: PhoneTurn | undefined, cur: PhoneTurn): boolean {
  if (!cur.at) return false;
  if (!prev || !prev.at) return !!cur.at && !prev;
  return new Date(prev.at * 1000).toDateString() !== new Date(cur.at * 1000).toDateString();
}

/** What a settled card's status line says. */
export function cardStatus(c: PhoneCard): { tone: "allow" | "deny" | "expired" | "queued" | ""; text: string } {
  switch (c.verdict) {
    case "allow":
      return { tone: "allow", text: c.kind === "question" ? "✓ Answered" : "✓ Allowed" };
    case "deny":
      return { tone: "deny", text: "✗ Denied" };
    case "held":
      return { tone: "queued", text: "Answered · waiting for the folder" };
    case "expired":
      return { tone: "expired", text: "Expired" };
    default:
      return { tone: "", text: "" };
  }
}

/** A set with `v` toggled. */
export function toggleIn<T>(set: ReadonlySet<T>, v: T): Set<T> {
  const n = new Set(set);
  if (n.has(v)) n.delete(v);
  else n.add(v);
  return n;
}

/** The cards worth drawing under the thread: every one still waiting (live or
 *  held), plus the most recently settled one so the verdict just given is
 *  seen; older settled cards fall away rather than pinning under every reply. */
export function visibleCards(cards: PhoneCard[]): PhoneCard[] {
  const waiting = cards.filter((c) => c.verdict === "pending" || c.verdict === "held");
  const settled = cards.filter((c) => c.verdict !== "pending" && c.verdict !== "held");
  const last = settled.length ? [settled.reduce((a, b) => (b.created_at >= a.created_at ? b : a))] : [];
  return [...last, ...waiting].sort((a, b) => a.created_at - b.created_at);
}

/** Does the settled answer name this label? The answer is a label, or the
 *  ", "-join of labels in option order; matched by construction, not substring. */
export function answerHas(answer: string | undefined, label: string, labels: string[]): boolean {
  if (!answer) return false;
  if (answer === label) return true;
  // Walk the option list: the answer must be a join of a subset in order.
  let rest = answer;
  const picked: string[] = [];
  for (const l of labels) {
    if (rest === l || rest.startsWith(l + ", ")) {
      picked.push(l);
      rest = rest === l ? "" : rest.slice(l.length + 2);
    }
  }
  return picked.includes(label) && (rest === "" || !labels.includes(rest));
}
