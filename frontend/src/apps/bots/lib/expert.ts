// Pure helpers for an expert's panels (components/ExpertPane.tsx) and its proposal card (components/ApprovalCard.tsx):
// ordering and the short strings the rows show. The shapes are docs/experts.md §4 (Item, Consult, Run, Proposal).
import type { ExpertArchived, ExpertCheckStatus, ExpertConsolidating, ExpertConsult, ExpertEngineRow, ExpertGap, ExpertLastTidy, ExpertMemory, ExpertObs, ExpertItem, ExpertProposal,
  ExpertScore } from "./api";

/** How often a lesson was used: the sessions that showed it plus the answers that relied on it (`cited`, absent on an
 *  older server: 0). */
export const lessonUses = (it: Pick<ExpertItem, "count" | "cited">): number => Math.max(0, it.count || 0) + Math.max(0, it.cited || 0);

/** Memory as the expert's own index reads it: most used first, then the most recently touched. */
export function sortLessons(items: readonly ExpertItem[]): ExpertItem[] {
  return [...items].sort((a, b) => lessonUses(b) - lessonUses(a) || (b.updated || 0) - (a.updated || 0) || a.id.localeCompare(b.id));
}

/** The proposals the Memory panel mentions (in progress, waiting on the user, or failed), newest first. */
export function openProposals(ps: readonly ExpertProposal[] | null | undefined): ExpertProposal[] {
  return (ps || []).filter((p) => ["preparing", "ready", "shown", "failed"].includes(p.status)).sort((a, b) => (b.ts || 0) - (a.ts || 0));
}

/** One open proposal as the Memory panel says it ("" for a status it does not mention). */
export function proposalLine(p: Pick<ExpertProposal, "status" | "error">): string {
  if (p.status === "preparing") return "Drafting a skill proposal…";
  if (p.status === "ready" || p.status === "shown") return "A skill proposal is waiting for your answer in the chat.";
  if (p.status === "failed") return `A skill proposal failed: ${p.error || "no reason given"}`;
  return "";
}

/** Whether a proposal is still being prepared (the pane polls while one is). */
export const anyPreparing = (ps: readonly ExpertProposal[] | null | undefined): boolean => !!ps?.some((p) => p.status === "preparing");

/** "used 3×" (lessonUses; a lesson in memory was seen at least once, so never "used 0×"). */
export const usedLabel = (it: Pick<ExpertItem, "count" | "cited">): string => `used ${Math.max(1, lessonUses(it))}×`;

/** The opened lesson card's counts: "Seen in 2 sessions · relied on in 5 answers" (the second part only when the
 *  server reports `cited`). */
export function usesLine(it: Pick<ExpertItem, "count" | "cited">): string {
  const seen = Math.max(1, it.count || 0), parts = [`Seen in ${seen} session${seen === 1 ? "" : "s"}`];
  if (typeof it.cited === "number") parts.push(`relied on in ${it.cited} answer${it.cited === 1 ? "" : "s"}`);
  return parts.join(" · ");
}

/** The Memory tab's budget line, or null when the server sends no `limit` (an older server). `total` falls back to
 *  the lessons listed, `tidy_at` to the limit. `fill` / `over`: the meter's neutral part (up to `tidy_at`) and its
 *  warning part (beyond it), as percentages of the bar. */
export interface BudgetState { total: number; limit: number; tidyAt: number; above: boolean; label: string; note: string; fill: number; over: number }
export function budgetState(m: Pick<ExpertMemory, "limit" | "tidy_at" | "total"> | null | undefined, listed = 0): BudgetState | null {
  const limit = m?.limit;
  if (typeof limit !== "number" || !Number.isFinite(limit) || limit <= 0) return null;
  const total = Math.max(0, typeof m?.total === "number" && Number.isFinite(m.total) ? m.total : listed);
  const tidyAt = typeof m?.tidy_at === "number" && Number.isFinite(m.tidy_at) ? Math.min(Math.max(0, m.tidy_at), limit) : limit;
  const above = total > tidyAt, bar = Math.max(limit, total), pct = (n: number) => Math.round((n / bar) * 1000) / 10;
  return {
    total, limit, tidyAt, above, label: `${total} of ${limit} lessons`,
    note: above ? `Above ${tidyAt}: each consolidation removes old, rarely used lessons and proposes heavily used ones as skills.`
      : `Tidying starts above ${tidyAt}.`,
    fill: pct(Math.min(total, tidyAt)), over: pct(Math.max(0, total - tidyAt)),
  };
}

/** Why a lesson left memory, in plain words (the "Recently removed" rows). */
export function removedReasonLabel(a: Pick<ExpertArchived, "reason" | "skill">): string {
  switch (a.reason) {
    case "stale": return "removed: old and rarely used";
    case "promoted": return a.skill ? `moved into the skill ${a.skill}` : "moved into a skill";
    case "over_limit": return "removed: memory was over its limit";
    case "forgotten": return "forgotten by you";
    default: return "removed";
  }
}

/** "Last tidy: removed 3, proposing 2 as skills." for a tidy under a day old ("" when older, absent, or it did
 *  nothing). Lessons cut for being over the limit count as removed. */
export function tidySummary(t: ExpertLastTidy | null | undefined, nowMs: number = Date.now()): string {
  if (!t || typeof t.at !== "number" || nowMs / 1000 - t.at >= 86400) return "";
  const removed = (t.deleted?.length || 0) + (t.over_limit?.length || 0), promoting = t.promoting?.length || 0;
  const parts = [removed ? `removed ${removed}` : "", promoting ? `proposing ${promoting} as skill${promoting === 1 ? "" : "s"}` : ""].filter(Boolean);
  return parts.length ? `Last tidy: ${parts.join(", ")}.` : "";
}

export type LessonStatus = "in-use" | "not-used" | "contradicted";
export type LessonFilter = "all" | "in-use" | "not-used";
export const LESSON_STATUS_LABEL: Record<LessonStatus, string> = { "in-use": "In use", "not-used": "Not used yet", contradicted: "Contradicted" };

/** A lesson card's status marker: "contradicted" when any session showed it wrong (wins), else whether answers draw on
 *  it (`indexed === false`: not used yet; absent reads as in use). */
export function lessonStatus(it: Pick<ExpertItem, "indexed" | "contradicted">): LessonStatus {
  if ((it.contradicted || 0) > 0) return "contradicted";
  return it.indexed === false ? "not-used" : "in-use";
}

/** Where a lesson came from, by its trust: "taught by you", "from sessions", "found by research" ("" otherwise). */
export function lessonSource(it: Pick<ExpertItem, "trust">): string {
  return it.trust === "user" ? "taught by you" : it.trust === "outcome" ? "from sessions" : it.trust === "research" ? "found by research" : "";
}

/** A lesson card's footer facts: where it stands on the way to a skill, "contradicted N×", and why a lesson is not
 *  used yet ("waiting for your approval" for a research finding, "seen once" otherwise). A lesson that goes into a
 *  skill leaves memory, so an older server's `promoted` lesson just shows no skill fact. */
export function lessonFacts(it: Pick<ExpertItem, "trust" | "promoted" | "proposed" | "rejected" | "indexed" | "contradicted">): string[] {
  const out: string[] = [];
  if (it.promoted) { /* nothing: never "in a skill" any more */ }
  else if (it.rejected) out.push("not proposed again");
  else if (it.proposed) out.push("skill proposal pending");
  if ((it.contradicted || 0) > 0) out.push(`contradicted ${it.contradicted}×`);
  if (it.indexed === false) out.push(it.trust === "research" ? "waiting for your approval" : "seen once");
  return out;
}

/** The lessons one filter chip keeps ("in-use": what answers draw on, contradicted ones included), in the order given. */
export function filterLessons(items: readonly ExpertItem[], f: LessonFilter): ExpertItem[] {
  if (f === "all") return [...items];
  return items.filter((it) => (it.indexed === false ? "not-used" : "in-use") === f);
}

/** Consults newest first (the route already sends them so; a merge or an older server may not). */
export const sortConsults = (cs: readonly ExpertConsult[]): ExpertConsult[] => [...cs].sort((a, b) => (b.ts || 0) - (a.ts || 0));

/** The last path segment: "/x/canvases/team/foo/" → "foo". */
export function basename(p: string | null | undefined): string {
  const t = (p || "").replace(/[\\/]+$/, "");
  return t.slice(Math.max(t.lastIndexOf("/"), t.lastIndexOf("\\")) + 1);
}

// ------------------------------------------------------------------ consult cards ----

export type ConsultStatus = "answered" | "unsure";
export type ConsultFilter = "all" | ConsultStatus;

/** A consult's outcome: "unsure" when the expert said it was not sure, else "answered". */
export const consultStatus = (c: Pick<ExpertConsult, "unsure">): ConsultStatus => (c.unsure ? "unsure" : "answered");
export const CONSULT_STATUS_LABEL: Record<ConsultStatus, string> = { answered: "Answered", unsure: "Unsure" };

/** Where a consult came from: "<target folder> · <backend>" (each as far as known), "direct" without a caller target. */
export function consultSource(c: Pick<ExpertConsult, "caller">): string {
  const target = basename(c.caller?.target);
  if (!target) return "direct";
  return [target, c.caller?.backend || ""].filter(Boolean).join(" · ");
}

/** A consult's length from milliseconds: "18s", "1m 5s", "2m"; "<1s" under half a second; "" when unknown. */
export function formatDuration(ms: number | null | undefined): string {
  if (!ms || ms < 0 || !Number.isFinite(ms)) return "";
  const s = Math.round(ms / 1000);
  if (s < 1) return "<1s";
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60), r = s % 60;
  return r ? `${m}m ${r}s` : `${m}m`;
}

/** The collapsed card's footer facts: "N files", "N lessons used", the duration (each only when there is one). */
export function consultFacts(c: Pick<ExpertConsult, "refs" | "used" | "ms">): string[] {
  const out: string[] = [], refs = c.refs?.length || 0, used = c.used?.length || 0, dur = formatDuration(c.ms);
  if (refs) out.push(`${refs} file${refs === 1 ? "" : "s"}`);
  if (used) out.push(`${used} lesson${used === 1 ? "" : "s"} used`);
  if (dur) out.push(dur);
  return out;
}

/** The consults one filter chip keeps ("all" keeps every one), in the order given. */
export function filterConsults(cs: readonly ExpertConsult[], f: ConsultFilter): ExpertConsult[] {
  return f === "all" ? [...cs] : cs.filter((c) => consultStatus(c) === f);
}

/** Consults newest first, under day dividers: "Today", "Yesterday", else a short date ("Sep 8", with the year when it
 *  is not this year). `key` is the local calendar day. Timestamps are epoch seconds. */
export function groupConsultsByDay(cs: readonly ExpertConsult[], nowMs: number = Date.now()): { key: string; label: string; items: ExpertConsult[] }[] {
  return groupByDay(sortConsults(cs), nowMs);
}

/** Items (already in display order) under day dividers, as groupConsultsByDay; a day split by other days opens again. */
export function groupByDay<T extends { ts: number }>(xs: readonly T[], nowMs: number = Date.now()): { key: string; label: string; items: T[] }[] {
  const now = new Date(nowMs), yest = new Date(now);
  yest.setDate(now.getDate() - 1);
  const dayKey = (d: Date) => `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`;
  const today = dayKey(now), yesterday = dayKey(yest);
  const out: { key: string; label: string; items: T[] }[] = [];
  for (const c of xs) {
    const d = new Date((c.ts || 0) * 1000), key = dayKey(d);
    let g = out[out.length - 1];
    if (!g || g.key !== key) {
      const label = key === today ? "Today" : key === yesterday ? "Yesterday"
        : d.toLocaleDateString([], { month: "short", day: "numeric", ...(d.getFullYear() !== now.getFullYear() ? { year: "numeric" } : {}) });
      g = { key, label, items: [] };
      out.push(g);
    }
    g.items.push(c);
  }
  return out;
}

/** Whether a consult's context is long enough to clamp (more than about 4 lines in the narrow panel). */
export const isLongContext = (t: string | null | undefined): boolean => !!t && (t.split("\n").length > 4 || t.length > 280);

/** The lessons a consult drew on, as their claims (from the memory items; the id itself when it is not found). */
export function usedLessons(used: readonly string[] | null | undefined, items: readonly Pick<ExpertItem, "id" | "claim">[] | null | undefined): { id: string; text: string }[] {
  const by = new Map((items || []).map((it) => [it.id, it.claim]));
  return (used || []).map((id) => ({ id, text: by.get(id) || id }));
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

/** The gap's skill-fix proposal status and error: the gap's own `proposal_status` / `proposal_error` when the server
 *  sends them, else its `proposal` looked up in `proposals`; status "" when neither says. */
export function gapProposal(g: Pick<ExpertGap, "proposal" | "proposal_status" | "proposal_error">,
  proposals: readonly Pick<ExpertProposal, "id" | "status" | "error">[] | null | undefined = null): { status: ExpertProposal["status"] | ""; error: string } {
  if (g.proposal_status) return { status: g.proposal_status, error: g.proposal_error || "" };
  const p = g.proposal ? (proposals || []).find((x) => x.id === g.proposal) : undefined;
  return p ? { status: p.status, error: p.error || g.proposal_error || "" } : { status: "", error: g.proposal_error || "" };
}

/** A gap's status tag in plain words. `researching`: the gap id the server says it is researching now (that gap reads
 *  "researching…" whatever its stored status). A `proposed` gap says where its proposal stands (gapProposal): a
 *  proposal is requested long before its card is up (drafting and two check runs), so "proposed in chat" waits for it. */
export function gapStatusLabel(g: Pick<ExpertGap, "id" | "status" | "resolution"> & Partial<Pick<ExpertGap, "proposal" | "proposal_status" | "proposal_error">>,
  researching: string | null = null, proposals: readonly Pick<ExpertProposal, "id" | "status" | "error">[] | null = null): string {
  if (researching && g.id === researching && gapUnresolved(g)) return "researching…";
  switch (g.status) {
    case "open": return "waiting";
    case "researching": return "researching…";
    case "proposed": {
      const st = gapProposal({ proposal: g.proposal ?? null, proposal_status: g.proposal_status, proposal_error: g.proposal_error }, proposals).status;
      if (st === "preparing") return "drafting a skill fix…";
      if (st === "ready") return "skill fix ready, card coming";
      if (st === "failed") return "skill fix failed";
      return "skill fix proposed in chat";
    }
    case "asked": return "asked you in chat";
    case "answered": return g.resolution === "user" ? "answered by you" : g.resolution === "skill" ? "answered by a new skill" : "answered";
    case "dismissed": return "dropped";
    default: return String(g.status || "");
  }
}

/** A gap's research note as shown: "not found: why" reads "Looked into it: why" (a bare "not found" too). */
export function gapNote(error: string | null | undefined): string {
  const t = (error || "").trim();
  if (t === "not found") return "Looked into it: nothing found";
  return /^not found: /.test(t) ? `Looked into it: ${t.slice("not found: ".length)}` : t;
}

/** An observation's text as its row shows it: an unsure row without its "Could not answer: " lead (its tag says so). */
export function observationText(o: Pick<ExpertObs, "kind" | "claim"> & { gap?: Pick<ExpertGap, "question"> | null }): string {
  const t = o.claim || o.gap?.question || "";
  return o.kind === "gap" ? t.replace(/^Could not answer:\s*/, "") : t;
}

/** "asked 3×". */
export const askedLabel = (count: number | null | undefined): string => `asked ${Math.max(1, count || 0)}×`;

// ------------------------------------------------------------------ observations ----

export const OBS_INTRO = "Everything I noted from consults and sessions. Consolidating turns these into lessons and skill fixes.";
export const OBS_EMPTY = "Nothing noted yet. Unsure answers and things I learn from sessions show up here.";

const OBS_TYPES: Record<string, string> = { gap: "unsure", repeated_request: "asked for", user_correction: "correction",
  error_then_fix: "fix that worked", advice_confirmed: "advice confirmed", advice_contradicted: "advice contradicted" };
/** An observation's type tag. The legacy kind `answer_contradicted` covered both outcomes of expert advice: it reads
 *  "advice contradicted" when consolidation counted it as a contradiction, "advice confirmed" for any other outcome,
 *  and "advice checked" while it has none. */
export function observationTypeLabel(kind: string | null | undefined, outcome?: ExpertObs["outcome"]): string {
  if (kind === "answer_contradicted") {
    if (!outcome?.op) return "advice checked";
    return outcome.op === "contradict" ? "advice contradicted" : "advice confirmed";
  }
  return OBS_TYPES[kind || ""] || "learning";
}

/** A consolidated observation's result tag: what consolidation made of it. A gap row says where its gap stands
 *  (gapStatusLabel; `researching` as there); "" when there is nothing to say (still pending, unknown op). */
export function observationResultLabel(o: Pick<ExpertObs, "kind" | "outcome" | "gap">, researching: string | null = null,
  proposals: readonly Pick<ExpertProposal, "id" | "status" | "error">[] | null = null): string {
  if (o.gap) return gapStatusLabel(o.gap, researching, proposals);
  switch (o.outcome?.op) {
    case "add": return "new lesson";
    case "bump": return "counted toward a lesson";
    case "contradict": return "contradicted a lesson";
    case "nothing": return "nothing new";
    case "gap": return o.kind === "gap" ? "waiting" : "";
    default: return "";
  }
}

/** Where an observation came from: the source folder and backend when known, else "consult" for one from a consult. */
export function observationSource(o: Pick<ExpertObs, "source" | "consult">): string {
  const target = basename(o.source?.target), backend = o.source?.backend || "";
  return target || backend ? [target, backend].filter(Boolean).join(" · ") : o.consult ? "consult" : "";
}

/** An observation type marker's tone: the warning token for an unsure answer or contradicted advice, the positive one
 *  for what worked or held up, the accent for your correction, muted for the rest. */
export function observationTone(kind: string | null | undefined, outcome?: ExpertObs["outcome"]): "warn" | "ok" | "accent" | "muted" {
  const label = observationTypeLabel(kind, outcome);
  if (label === "unsure" || label === "advice contradicted") return "warn";
  if (label === "fix that worked" || label === "advice confirmed") return "ok";
  if (label === "correction") return "accent";
  return "muted";
}

/** An observation card's footer: for a waiting row "waiting to be consolidated" (a gap already past "open" says where
 *  it stands instead); for a consolidated one what came of it (observationResultLabel); then "asked N×" for a gap asked
 *  more than once. */
export function observationFacts(o: Pick<ExpertObs, "kind" | "outcome" | "gap">, consolidated: boolean, researching: string | null = null,
  proposals: readonly Pick<ExpertProposal, "id" | "status" | "error">[] | null = null): string[] {
  const out: string[] = [], g = o.gap;
  const gapMoved = !!g && (g.status !== "open" || (researching != null && researching === g.id));
  const result = !consolidated && !gapMoved ? "waiting to be consolidated" : observationResultLabel(o, researching, proposals);
  if (result) out.push(result);
  if (g && (g.count || 0) > 1) out.push(askedLabel(g.count));
  return out;
}

export type ObservationFilter = "all" | "unsure" | "learnings";
/** The observations one filter chip keeps: "unsure" (the expert could not answer), "learnings" (everything else). */
export function filterObservations(os: readonly ExpertObs[], f: ObservationFilter): ExpertObs[] {
  if (f === "all") return [...os];
  return os.filter((o) => (o.kind === "gap") === (f === "unsure"));
}

/** The tab's two sections: waiting (oldest first) and consolidated (newest first). */
export function sortObservations(pending: readonly ExpertObs[] | null | undefined, done: readonly ExpertObs[] | null | undefined): { waiting: ExpertObs[]; done: ExpertObs[] } {
  return { waiting: [...(pending || [])].sort((a, b) => (a.ts || 0) - (b.ts || 0)), done: [...(done || [])].sort((a, b) => (b.ts || 0) - (a.ts || 0)) };
}

/** The gaps joined into the observations (each once), for "is an open question left". */
export function observationGaps(...lists: (readonly ExpertObs[] | null | undefined)[]): ExpertGap[] {
  const seen = new Map<string, ExpertGap>();
  for (const l of lists) for (const o of l || []) if (o.gap && !seen.has(o.gap.id)) seen.set(o.gap.id, o.gap);
  return [...seen.values()];
}

/** The gap a running consolidation is researching now, if any. */
export const researchingGap = (c: ExpertConsolidating | null | undefined): string | null =>
  c?.running && (c.stage === "researching" || c.stage === "proposing") ? c.gap || null : null;

/** While a consolidation runs: what it is doing, by stage ("" when it is not running). */
export function consolidatingLine(c: ExpertConsolidating | null | undefined): string {
  if (!c?.running) return "";
  switch (c.stage) {
    case "lessons": return "Consolidating lessons…";
    case "tidying": return "Tidying memory…";
    case "gaps": return "Sorting open questions…";
    case "researching": return "Researching an open question…";
    case "proposing": return "Drafting a skill fix…";
    default: return "Consolidating…";
  }
}
