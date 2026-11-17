// The expert pane's pure helpers (components/ExpertPane.tsx): a session row's words (kind, origin from its trigger,
// status, time), the memory size line, and a unified diff split into coloured lines. No fetching, no React.
import type { ExpertMemory, ExpertSession, ExpertTrigger } from "./api";
import { fmtBytes, fmtDur } from "./format";

/** The kinds as the pane names them; an unknown kind shows as sent. */
export const KIND_LABEL: Record<string, string> = { chat: "chat", ask: "ask", "task-quiet": "task-quiet", compact: "compact" };
export const kindLabel = (k: string): string => KIND_LABEL[k] || k || "session";

/** The status as a word and a tone (running: accent, done: ok, error: err, interrupted: warn). */
export function statusOf(s: Pick<ExpertSession, "status">): { text: string; tone: "accent" | "ok" | "err" | "warn" | "muted" } {
  switch (s.status) {
    case "running": return { text: "running", tone: "accent" };
    case "done": return { text: "done", tone: "ok" };
    case "error": return { text: "error", tone: "err" };
    case "interrupted": return { text: "interrupted", tone: "warn" };
    default: return { text: String(s.status || "unknown"), tone: "muted" };
  }
}

/** One line, collapsed whitespace, cut to `n` characters with an ellipsis. */
export const oneLine = (s: unknown, n = 80): string => {
  const t = String(s ?? "").replace(/\s+/g, " ").trim();
  return t.length > n ? t.slice(0, n - 1).trimEnd() + "…" : t;
};

/** The last path segment ("/Users/a/Fused/canvas/x/" -> "x"). */
export const basename = (p: string): string => p.replace(/\/+$/, "").split("/").pop() || p;

/** Where a session came from, in one line, read from its trigger: the chat message; who asked and the question; the
 *  task's target folder (else its backend); compact: the size that set it off. "" when the trigger says nothing. */
export function originOf(kind: string, t: ExpertTrigger | null | undefined): string {
  const g = t || {};
  if (kind === "chat") return g.message ? `“${oneLine(g.message, 70)}”` : "your chat";
  if (kind === "ask") {
    const who = oneLine(g.asker, 40), q = oneLine(g.question, 70);
    return who && q ? `${who}: ${q}` : who || q;
  }
  if (kind === "task-quiet") {
    const where = g.target ? basename(String(g.target)) : "", be = oneLine(g.backend, 20);
    return where ? `task in ${where}${be ? ` (${be})` : ""}` : be ? `${be} task` : "a task";
  }
  if (kind === "compact") {
    const parts = [typeof g.lines === "number" ? `${g.lines} lines` : "", typeof g.bytes === "number" ? fmtBytes(g.bytes) : ""].filter(Boolean);
    return parts.length ? `memory at ${parts.join(", ")}` : "memory over the limit";
  }
  return "";
}

/** Did this session change memory.md? (The change log is the sessions where this holds.) */
export const changedMemory = (s: Pick<ExpertSession, "memory_diff">): boolean => !!(s.memory_diff || "").trim();

/** How long it ran ("12 s", "3 min"); "" while running or under a second. */
export const duration = (s: Pick<ExpertSession, "started" | "ended">): string => (s.ended && s.started ? fmtDur(s.ended - s.started) : "");

/** The list's filter: every session, or only the ones that changed memory. Newest first either way (the server
 *  sends them so; sorted again here so a merged list cannot come out of order). */
export type SessionFilter = "all" | "changed";
export function filterSessions(rows: readonly ExpertSession[], f: SessionFilter): ExpertSession[] {
  return rows.filter((s) => f === "all" || changedMemory(s)).sort((a, b) => (b.started || 0) - (a.started || 0));
}

export type DiffTone = "add" | "del" | "hunk" | "meta" | "ctx";
export interface DiffLine { tone: DiffTone; text: string }

/** A unified diff as lines with a tone: `+` added, `-` removed, `@@` a hunk header, the `---` / `+++` file headers
 *  (and `\ No newline…`) as meta, the rest context. An empty diff is no lines. */
export function diffLines(diff: string | null | undefined): DiffLine[] {
  const text = (diff || "").replace(/\n$/, "");
  if (!text.trim()) return [];
  return text.split("\n").map((l): DiffLine => {
    if (l.startsWith("+++") || l.startsWith("---") || l.startsWith("\\")) return { tone: "meta", text: l };
    if (l.startsWith("@@")) return { tone: "hunk", text: l };
    if (l.startsWith("+")) return { tone: "add", text: l };
    if (l.startsWith("-")) return { tone: "del", text: l };
    return { tone: "ctx", text: l };
  });
}

/** "+3 −1": lines added and removed (file headers not counted); "" for no change. */
export function diffStat(diff: string | null | undefined): string {
  let add = 0, del = 0;
  for (const l of diffLines(diff)) { if (l.tone === "add") add++; else if (l.tone === "del") del++; }
  return add || del ? `+${add} −${del}` : "";
}

/** memory.md's size against the compaction limit: "42 of 200 lines · 3 KB of 12 KB", and whether either is over. */
export function memoryUsage(m: Pick<ExpertMemory, "lines" | "bytes" | "limit_lines" | "limit_bytes">): { text: string; over: boolean } {
  const lines = m.lines || 0, bytes = m.bytes || 0;
  const l = m.limit_lines ? `${lines} of ${m.limit_lines} lines` : `${lines} line${lines === 1 ? "" : "s"}`;
  const b = m.limit_bytes ? `${fmtBytes(bytes)} of ${fmtBytes(m.limit_bytes)}` : fmtBytes(bytes);
  return { text: `${l} · ${b}`, over: (!!m.limit_lines && lines > m.limit_lines) || (!!m.limit_bytes && bytes > m.limit_bytes) };
}

/** What the harness reported the session cost, " · $0.03"; "" when it reported nothing. */
export const costLabel = (usd: number | null | undefined): string =>
  typeof usd === "number" && usd > 0 ? ` · $${usd < 0.01 ? usd.toFixed(3) : usd.toFixed(2)}` : "";

/** A transcript role as its tag ("prompt", "expert", "tool", "refused"; anything else as sent). */
export const roleTag = (r: string): string => r || "?";
