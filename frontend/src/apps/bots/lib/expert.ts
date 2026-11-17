// Pure helpers for an expert's panels (components/ExpertPane.tsx): ordering and the short strings the cards show.
// The shapes are docs/experts.md (the activity line, the lesson, the consult).
import type { ExpertActivity, ExpertBudget, ExpertConsult, ExpertEngineRow, ExpertEvidence, ExpertItem, ExpertLastTidy, ExpertLearning } from "./api";

/** How often a lesson was used: the sessions that showed it plus the answers that relied on it (`cited`, absent on an
 *  older server: 0). */
export const lessonUses = (it: Pick<ExpertItem, "count" | "cited">): number => Math.max(0, it.count || 0) + Math.max(0, it.cited || 0);

/** Memory as the expert's own index reads it: most used first, then the most recently touched. */
export function sortLessons(items: readonly ExpertItem[]): ExpertItem[] {
  return [...items].sort((a, b) => lessonUses(b) - lessonUses(a) || (b.updated || 0) - (a.updated || 0) || a.id.localeCompare(b.id));
}

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
export function budgetState(m: Pick<ExpertBudget, "limit" | "tidy_at" | "total"> | null | undefined, listed = 0): BudgetState | null {
  const limit = m?.limit;
  if (typeof limit !== "number" || !Number.isFinite(limit) || limit <= 0) return null;
  const total = Math.max(0, typeof m?.total === "number" && Number.isFinite(m.total) ? m.total : listed);
  const tidyAt = typeof m?.tidy_at === "number" && Number.isFinite(m.tidy_at) ? Math.min(Math.max(0, m.tidy_at), limit) : limit;
  const above = total > tidyAt, bar = Math.max(limit, total), pct = (n: number) => Math.round((n / bar) * 1000) / 10;
  return {
    total, limit, tidyAt, above, label: `${total} of ${limit} lessons`,
    note: above ? `Above ${tidyAt}: it removes old, rarely used lessons, merges related ones, and moves heavily used ones into skills.`
      : `Tidying starts above ${tidyAt}.`,
    fill: pct(Math.min(total, tidyAt)), over: pct(Math.max(0, total - tidyAt)),
  };
}

/** "Last tidy: removed 3, merged 4 into 2, moved 2 into skills." for a tidy under a day old ("" when older, absent,
 *  or it did nothing). Lessons cut for being over the limit count as removed; merged: the originals into the lessons
 *  they became. */
export function tidySummary(t: ExpertLastTidy | null | undefined, nowMs: number = Date.now()): string {
  if (!t || typeof t.at !== "number" || nowMs / 1000 - t.at >= 86400) return "";
  const removed = (t.deleted?.length || 0) + (t.over_limit?.length || 0), promoting = t.promoting?.length || 0;
  const merges = (t.merged || []).filter(Boolean), into = merges.length, merged = merges.reduce((n, m) => n + (m.from?.length || 0), 0);
  const parts = [removed ? `removed ${removed}` : "", into && merged ? `merged ${merged} into ${into}` : "", promoting ? `moved ${promoting} into skill${promoting === 1 ? "" : "s"}` : ""].filter(Boolean);
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

/** Where a lesson came from, by its trust: "taught by you" ("you said this in a session" for a correction verified in
 *  a task session, `via: "session"`), "from sessions", "found by research" ("" otherwise). */
export function lessonSource(it: Pick<ExpertItem, "trust"> & Partial<Pick<ExpertItem, "via">>): string {
  if (it.trust === "user") return it.via === "session" ? "you said this in a session" : "taught by you";
  return it.trust === "outcome" ? "from sessions" : it.trust === "research" ? "found by research" : "";
}

/** A lesson card's footer facts: "merged from N", "contradicted N×", "confirmed N×" (sessions that echoed the expert's
 *  own advice; they do not raise its count), and "seen once" for a lesson answers do not draw on yet. */
export function lessonFacts(it: Pick<ExpertItem, "indexed" | "contradicted" | "merged_from"> & Partial<Pick<ExpertItem, "confirmed">>): string[] {
  const out: string[] = [];
  const merged = Array.isArray(it.merged_from) ? it.merged_from.length : 0;
  if (merged) out.push(`merged from ${merged}`);
  if ((it.contradicted || 0) > 0) out.push(`contradicted ${it.contradicted}×`);
  if ((it.confirmed || 0) > 0) out.push(`confirmed ${it.confirmed}×`);
  if (it.indexed === false) out.push("seen once");
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

/** The Engine field's helper text: how Auto resolves (model.py `AUTO_ENGINES`: Claude if on, else Copilot; never
 *  Codex, whose reads Fused does not fence). */
export const ENGINE_HELP = "Who answers for this expert. Auto uses Claude when it is on, otherwise Copilot; it never picks Codex. Saved at once.";

/** The Learning switch's one line of help (Settings › Expert). */
export const LEARNING_HELP = "Off: it still answers and notes what happens, and changes nothing.";

const ENGINE_LABELS: Record<string, string> = { claude: "Claude", codex: "Codex", copilot: "Copilot" };

/** The Engine menu: Auto, then each engine that is switched on (an engine that is off cannot be picked). The current
 *  value stays listed even when it went off, or before the engines arrive (`rows` empty), so the menu never silently
 *  shows another one (and a change never writes Auto by accident). */
export function engineOptions(rows: readonly ExpertEngineRow[], current: string): [string, string][] {
  const out: [string, string][] = [["auto", "Auto"]];
  for (const r of rows) if (r.id !== "auto" && (r.enabled || r.id === current)) out.push([r.id, r.enabled ? r.label : `${r.label} (off)`]);
  if (current && !out.some(([v]) => v === current)) out.push([current, ENGINE_LABELS[current] || current]);
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

// ------------------------------------------------------------------ activity ----

const ACTION_LABEL: Record<string, string> = { learned: "Learned", counted: "Seen again", corrected: "Corrected", forgot: "Forgot", merged: "Merged",
  removed: "Removed", skill_updated: "Skill updated", skill_created: "Skill created", skill_restored: "Skill restored", noticed: "Noticed",
  researched: "Researched", could_not_find: "Could not find out", skipped: "Skipped", undo: "Undo" };
/** What the expert did, in plain words ("Skill updated"; an action this page does not know reads as itself, spaced). */
export function actionLabel(action: string | null | undefined): string {
  const a = action || "";
  if (ACTION_LABEL[a]) return ACTION_LABEL[a];
  const t = a.replace(/_/g, " ").trim();
  return t ? t[0].toUpperCase() + t.slice(1) : "Did something";
}

/** The marker's tone: the warning token for a change that failed or something it could not do, the positive one for
 *  what it now knows, the accent for a skill change, muted for the rest (and for anything undone). */
export function actionTone(a: Pick<ExpertActivity, "action" | "ok"> & Partial<Pick<ExpertActivity, "undone">>): "ok" | "warn" | "muted" | "accent" {
  if (a.ok === false || a.action === "could_not_find") return "warn";
  if (a.undone) return "muted";
  if (a.action.startsWith("skill_")) return "accent";
  return a.action === "learned" || a.action === "corrected" || a.action === "researched" ? "ok" : "muted";
}

const SKILL_TARGET_ACTIONS = new Set(["skill_updated", "skill_created", "skill_restored"]);
/** What the card header names beside the action: the skill a skill change touched. "" for every other line, whose
 *  `target` is an internal id (a lesson's, or for an undo the line it took back). */
export function activityTarget(a: Pick<ExpertActivity, "action"> & Partial<Pick<ExpertActivity, "target">>): string {
  return SKILL_TARGET_ACTIONS.has(a.action) ? (a.target || "").trim() : "";
}

export type ActivityFilter = "all" | "skills" | "memory";
const MEMORY_ACTIONS = new Set(["learned", "counted", "corrected", "forgot", "merged", "removed"]);
/** Which chip a line belongs under: a skill change, a memory change, or neither (notes, dead ends and undos show under All only). */
export function activityGroup(a: Pick<ExpertActivity, "action">): "skills" | "memory" | "" {
  return a.action.startsWith("skill_") ? "skills" : MEMORY_ACTIONS.has(a.action) ? "memory" : "";
}
export function filterActivity(xs: readonly ExpertActivity[], f: ActivityFilter): ExpertActivity[] {
  return f === "all" ? [...xs] : xs.filter((a) => activityGroup(a) === f);
}

/** A quiet line (seen again, noticed): hidden unless "Show everything" is on. */
export const isQuiet = (a: Pick<ExpertActivity, "action"> & Partial<Pick<ExpertActivity, "quiet">>): boolean =>
  a.quiet === true || (a.quiet == null && (a.action === "counted" || a.action === "noticed"));
/** The lines as listed: newest first, without the quiet ones unless `everything`. */
export function shownActivity(xs: readonly ExpertActivity[], everything: boolean): ExpertActivity[] {
  return [...xs].filter((a) => everything || !isQuiet(a)).sort((a, b) => (b.ts || 0) - (a.ts || 0));
}

/** Whether a line still has an Undo: it changed something, worked, and was not taken back (an undo is never undone). */
export const canUndo = (a: Pick<ExpertActivity, "action" | "ok" | "undoable" | "undone">): boolean =>
  !!a.undoable && !a.undone && a.ok !== false && a.action !== "undo";

/** "3 things waiting to be learned" ("" for none). */
export function waitingLine(n: number | null | undefined): string {
  const w = Math.max(0, Math.floor(n || 0));
  return w ? `${w} thing${w === 1 ? "" : "s"} waiting to be learned` : "";
}

/** While it learns: what it is doing, by step ("" when it is not). */
export function learningLine(l: ExpertLearning | null | undefined): string {
  if (!l?.running) return "";
  switch (l.stage) {
    case "lessons": return "Going through what it noticed…";
    case "research": return "Looking into open questions…";
    case "distill": return "Updating skills…";
    case "tidy": return "Tidying memory…";
    default: return "Learning…";
  }
}

/** A line's evidence as the opened card shows it: the user's words, where it came from (the session's folder or canvas
 *  and what ran it; "a session" when only the session is known), and the file it was read in. */
export function evidenceParts(e: ExpertEvidence | null | undefined): { quote: string; from: string; fromTitle: string; file: string } {
  const src = e?.source, o = src && typeof src === "object" ? src : null;
  const file = typeof src === "string" ? src.trim() : (o?.path || "").trim();
  const where = basename(o?.canvas) || basename(o?.target), from = [where, o?.backend || ""].filter(Boolean).join(" · ") || (e?.session ? "a session" : "");
  return { quote: (e?.quote || "").trim(), from, fromTitle: [o?.canvas || o?.target || "", e?.session ? `session ${e.session}` : ""].filter(Boolean).join("\n"), file };
}

/** The message a failed undo shows on its card: the server's `detail` (a 409 says why), else the error's own text. */
export function undoError(e: unknown): string {
  const body = (e as { body?: Record<string, unknown> | null } | null)?.body;
  const d = body && typeof body.detail === "string" ? body.detail.trim() : "";
  return d || (e instanceof Error ? e.message : String(e || "")) || "Could not undo this.";
}
