// Pure helpers for an expert's panels (components/ExpertPane.tsx) and its proposal card (components/ApprovalCard.tsx):
// ordering and the short strings the rows show. The shapes are docs/experts.md §4 (Item, Consult, Run, Proposal).
import type { ExpertCheckStatus, ExpertConsolidating, ExpertConsult, ExpertEngineRow, ExpertGap, ExpertItem, ExpertProposal, ExpertScore } from "./api";
import { fmtAgo } from "./format";

/** Memory as the expert's own index reads it: most seen first, then the most recently touched. */
export function sortLessons(items: readonly ExpertItem[]): ExpertItem[] {
  return [...items].sort((a, b) => (b.count || 0) - (a.count || 0) || (b.updated || 0) - (a.updated || 0) || a.id.localeCompare(b.id));
}

/** Memory split for the panel: the lessons answers draw on, then the ones not used yet (`indexed === false`: seen once,
 *  or a research finding waiting for its proposal's approval) under their own divider. Each part sorted as `sortLessons`. */
export function splitLessons(items: readonly ExpertItem[]): { indexed: ExpertItem[]; seenOnce: ExpertItem[] } {
  const sorted = sortLessons(items);
  return { indexed: sorted.filter((it) => it.indexed !== false), seenOnce: sorted.filter((it) => it.indexed === false) };
}

export const SEEN_ONCE_LINE = "Not used in answers yet";

/** The proposals the Memory panel mentions (in progress, waiting on the user, or failed), newest first. */
export function openProposals(ps: readonly ExpertProposal[] | null | undefined): ExpertProposal[] {
  return (ps || []).filter((p) => ["preparing", "ready", "shown", "failed"].includes(p.status)).sort((a, b) => (b.ts || 0) - (a.ts || 0));
}

/** One open proposal as the Memory panel says it ("" for a status it does not mention). */
export function proposalLine(p: Pick<ExpertProposal, "status" | "error">): string {
  if (p.status === "preparing") return "Drafting a skill proposal and running the checks…";
  if (p.status === "ready" || p.status === "shown") return "A skill proposal is waiting for your answer in the chat.";
  if (p.status === "failed") return `A skill proposal failed: ${p.error || "no reason given"}`;
  return "";
}

/** Whether a proposal is still being prepared (the pane polls while one is). */
export const anyPreparing = (ps: readonly ExpertProposal[] | null | undefined): boolean => !!ps?.some((p) => p.status === "preparing");

/** "seen 3×" (a lesson seen once still says so: the count is what earns a proposal). */
export const seenLabel = (count: number | null | undefined): string => `seen ${Math.max(1, count || 0)}×`;

/** The markers after a lesson's claim: who said it (`user`, or "found by research"), why it is not used in answers yet
 *  (`indexed === false`: "waiting for your approval" for a research finding, "seen once" otherwise) and where it stands
 *  on the way to a skill (one of "promoted", "not proposed again" for a rejected proposal, or "proposed", in that priority). */
export function lessonTags(it: Pick<ExpertItem, "trust" | "promoted" | "proposed" | "rejected" | "indexed">): string[] {
  const out: string[] = [];
  if (it.trust === "user") out.push("user");
  else if (it.trust === "research") out.push("found by research");
  if (it.indexed === false) out.push(it.trust === "research" ? "waiting for your approval" : "seen once");
  if (it.promoted) out.push("promoted");
  else if (it.rejected) out.push("not proposed again");
  else if (it.proposed) out.push("proposed");
  return out;
}

/** The Memory tab's line about observations not yet folded into lessons. */
export function pendingLine(n: number, due: boolean): string {
  if (!n) return "No observations waiting.";
  return `${n} observation${n === 1 ? "" : "s"} waiting to be consolidated${due ? " (due now)" : ""}.`;
}

/** Consults newest first (the route already sends them so; a merge or an older server may not). */
export const sortConsults = (cs: readonly ExpertConsult[]): ExpertConsult[] => [...cs].sort((a, b) => (b.ts || 0) - (a.ts || 0));

/** The last path segment: "/x/canvases/team/foo/" → "foo". */
export function basename(p: string | null | undefined): string {
  const t = (p || "").replace(/[\\/]+$/, "");
  return t.slice(Math.max(t.lastIndexOf("/"), t.lastIndexOf("\\")) + 1);
}

/** A consult's quiet meta line: when, the caller's target folder and its backend, when known. */
export function consultMeta(c: Pick<ExpertConsult, "ts" | "caller">, nowMs: number = Date.now()): string {
  const target = basename(c.caller?.target), backend = c.caller?.backend || "";
  return [fmtAgo(c.ts, nowMs), target, backend].filter(Boolean).join(" · ");
}

/** "11 / 14" (the Checks tab's headline). */
export const scoreText = (s: ExpertScore | null | undefined): string => (s ? `${s.score} / ${s.total}` : "");

/** The proposal card's scores line, "Checks: 11/14 → 12/14"; "" unless both runs exist (no questions = no gate). */
export function scoresLine(before: ExpertScore | null | undefined, after: ExpertScore | null | undefined): string {
  if (!before || !after) return "";
  return `Checks: ${before.score}/${before.total} → ${after.score}/${after.total}`;
}

/** A proposal event's text as the card shows it: without its "Checks: …" lines (the card's scores line says that) and
 *  a trailing "Approve?" line (the buttons ask it). */
export function proposalText(text: string | null | undefined): string {
  const lines = (text || "").split("\n").filter((l) => !l.trim().startsWith("Checks:"));
  while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
  if (lines.length && lines[lines.length - 1].trim() === "Approve?") lines.pop();
  return lines.join("\n").replace(/\n{3,}/g, "\n\n").trim();
}

/** While a gate run is going: "Running checks · 3 of 14". */
export function checkProgress(st: ExpertCheckStatus | null | undefined): string {
  if (!st?.running) return "";
  return st.total ? `Running checks · ${st.done} of ${st.total}` : "Running checks…";
}

/** The Engine menu: Auto, then each engine that is switched on (an engine that is off cannot be picked). The current
 *  value stays listed even when it went off, so the menu never silently shows another one. */
export function engineOptions(rows: readonly ExpertEngineRow[], current: string): [string, string][] {
  const out: [string, string][] = [["auto", "Auto"]];
  for (const r of rows) if (r.id !== "auto" && (r.enabled || r.id === current)) out.push([r.id, r.enabled ? r.label : `${r.label} (off)`]);
  return out;
}

/** The Model menu for `engine`: "" (its default, named when known), then its models. Auto has only the default. */
export function modelOptions(rows: readonly ExpertEngineRow[], engine: string, current: string): [string, string][] {
  const row = rows.find((r) => r.id === engine);
  const def = row?.default_model ? row.models.find((m) => m.id === row.default_model)?.label || row.default_model : "";
  const out: [string, string][] = [["", def ? `Default (${def})` : "Default"]];
  for (const m of row?.models || []) out.push([m.id, m.label || m.id]);
  if (current && !out.some(([v]) => v === current)) out.push([current, current]);
  return out;
}

/** The selected engine's caveat (its row's `note`), shown under the Engine menu; "" when it has none. */
export function engineNote(rows: readonly ExpertEngineRow[], engine: string): string {
  return (rows.find((r) => r.id === engine)?.note || "").trim();
}

// ------------------------------------------------------------------ gaps ----

const UNRESOLVED = new Set<string>(["open", "researching", "proposed", "asked"]);
/** Whether a gap still waits on something (open, researching, or its proposal / question is up in the chat). */
export const gapUnresolved = (g: Pick<ExpertGap, "status">): boolean => UNRESOLVED.has(g.status);

/** How many gaps still wait on something (open questions; the Consolidate button counts them as work). */
export const unresolvedGapCount = (gs: readonly Pick<ExpertGap, "status">[] | null | undefined): number => (gs || []).filter(gapUnresolved).length;

/** A gap's status tag in plain words. `researching`: the gap id the server says it is researching now (that gap reads
 *  "researching…" whatever its stored status). */
export function gapStatusLabel(g: Pick<ExpertGap, "id" | "status" | "resolution">, researching: string | null = null): string {
  if (researching && g.id === researching && gapUnresolved(g)) return "researching…";
  switch (g.status) {
    case "open": return "waiting";
    case "researching": return "researching…";
    case "proposed": return "skill proposal in chat";
    case "asked": return "asked you in chat";
    case "answered": return g.resolution === "user" ? "answered by you" : g.resolution === "skill" ? "answered by a new skill" : "answered";
    case "dismissed": return "dropped";
    default: return String(g.status || "");
  }
}

/** The Gaps panel's two parts: unresolved (most asked first, then the most recently asked), then resolved (most
 *  recently touched first). */
export function splitGaps(gs: readonly ExpertGap[] | null | undefined): { unresolved: ExpertGap[]; resolved: ExpertGap[] } {
  const all = [...(gs || [])];
  return {
    unresolved: all.filter(gapUnresolved).sort((a, b) => (b.count || 0) - (a.count || 0) || (b.updated || 0) - (a.updated || 0) || a.id.localeCompare(b.id)),
    resolved: all.filter((g) => !gapUnresolved(g)).sort((a, b) => (b.updated || 0) - (a.updated || 0) || a.id.localeCompare(b.id)),
  };
}

/** "asked 3×". */
export const askedLabel = (count: number | null | undefined): string => `asked ${Math.max(1, count || 0)}×`;

export const GAPS_INTRO = "Questions sessions asked that I could not answer. Consolidating looks into each one, then proposes a skill fix or asks you.";

/** While a consolidation runs: what it is doing, by stage ("" when it is not running). */
export function consolidatingLine(c: ExpertConsolidating | null | undefined): string {
  if (!c?.running) return "";
  switch (c.stage) {
    case "lessons": return "Consolidating lessons…";
    case "gaps": return "Sorting open questions…";
    case "researching": return "Researching an open question…";
    case "proposing": return "Drafting a skill fix…";
    default: return "Consolidating…";
  }
}
