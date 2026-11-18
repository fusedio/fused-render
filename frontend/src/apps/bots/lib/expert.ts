// The expert pane's pure helpers (components/ExpertPane.tsx): a session row's words (kind, origin from its trigger,
// status, time), the memory size line, and a unified diff of memory.md read as rows of a document (added, removed,
// a removed + added pair, unchanged context). No fetching, no React.
import type { ExpertMemory, ExpertSession, ExpertTrigger } from "./api";
import { fmtBytes, fmtDur } from "./format";

/** The kinds as the pane names them; an unknown kind shows as sent. */
export const KIND_LABEL: Record<string, string> = { chat: "chat", ask: "ask", "task-learn": "task-learn", dream: "dream" };
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
 *  task's target folder (else its backend); dream (compaction): the size that set it off. "" when the trigger says nothing. */
export function originOf(kind: string, t: ExpertTrigger | null | undefined): string {
  const g = t || {};
  if (kind === "chat") return g.message ? `“${oneLine(g.message, 70)}”` : "your chat";
  if (kind === "ask") {
    const who = oneLine(g.asker, 40), q = oneLine(g.question, 70);
    return who && q ? `${who}: ${q}` : who || q;
  }
  if (kind === "task-learn") {
    const where = g.target ? basename(String(g.target)) : "", be = oneLine(g.backend, 20);
    return where ? `task in ${where}${be ? ` (${be})` : ""}` : be ? `${be} task` : "a task";
  }
  if (kind === "dream") {
    const parts = [typeof g.lines === "number" ? `${g.lines} lines` : "", typeof g.bytes === "number" ? fmtBytes(g.bytes) : ""].filter(Boolean);
    return parts.length ? `memory at ${parts.join(", ")}` : "memory over the limit";
  }
  return "";
}

/** Did this session change memory.md? (The change log is the sessions where this holds.) */
export const changedMemory = (s: Pick<ExpertSession, "memory_diff">): boolean => !!(s.memory_diff || "").trim();

/** How long it ran ("12 s", "3 min"); "" while running or under a second. */
export const duration = (s: Pick<ExpertSession, "started" | "ended">): string => (s.ended && s.started ? fmtDur(s.ended - s.started) : "");

/** Newest first (the server sends them so; sorted again here so a merged list cannot come out of order). */
export const newestFirst = (rows: readonly ExpertSession[]): ExpertSession[] => [...rows].sort((a, b) => (b.started || 0) - (a.started || 0));

/** One row of a memory diff read as a document: an added, removed or unchanged line (its markdown source, without the
 *  diff's one-character prefix); a removed line answered by an added one (a fact's count bump); or a gap between hunks. */
export type DiffRow =
  | { tone: "add" | "del" | "ctx"; text: string }
  | { tone: "pair"; del: string; add: string }
  | { tone: "gap" };

/** A fact's text without its `- [count · date]` stamp, to tell a bumped fact from a different one. */
const factOf = (l: string): string => l.replace(/^\s*[-*+]\s+\[[^\]]*\]\s*/, "").trim();

/** A unified diff of memory.md as document rows. The noise goes: everything before the first `@@` (the `---` / `+++`
 *  file headers, `diff` / `index` lines), each `@@` hunk header (a later one becomes a gap), `\ No newline…`.
 *  Inside a hunk the first character decides: `+` added, `-` removed, the rest context (so a removed `---` line is a
 *  line, not a header). A run of removed lines answered by a run of added lines pairs up: one and one always; otherwise
 *  each removed fact with the first added line holding the same fact (only its `[count · date]` stamp moved).
 *  Added or removed blank lines are left out; an empty diff, or one that only changed blank lines, is no rows. */
export function diffDoc(diff: string | null | undefined): DiffRow[] {
  const text = (diff || "").replace(/\r/g, "").replace(/\n$/, "");
  if (!text.trim()) return [];
  let lines = text.split("\n");
  const firstHunk = lines.findIndex((l) => l.startsWith("@@"));
  if (firstHunk >= 0) lines = lines.slice(firstHunk);
  else while (lines.length && /^(---|\+\+\+|diff |index )/.test(lines[0])) lines = lines.slice(1);
  const flat: DiffRow[] = [];
  let hunks = 0;
  for (const l of lines) {
    if (l.startsWith("@@")) { if (hunks++ && flat.length) flat.push({ tone: "gap" }); continue; }
    if (l.startsWith("\\")) continue;
    // A blank line added or removed is no fact: its row would be an empty coloured strip, so it is left out.
    if (l.startsWith("+")) { if (l.slice(1).trim()) flat.push({ tone: "add", text: l.slice(1) }); }
    else if (l.startsWith("-")) { if (l.slice(1).trim()) flat.push({ tone: "del", text: l.slice(1) }); }
    else flat.push({ tone: "ctx", text: l.startsWith(" ") ? l.slice(1) : l });
  }
  // Pair a removed run with the added run right after it (see above).
  const out: DiffRow[] = [];
  for (let i = 0; i < flat.length;) {
    if (flat[i].tone !== "del") { out.push(flat[i++]); continue; }
    let j = i;
    while (j < flat.length && flat[j].tone === "del") j++;
    let k = j;
    while (k < flat.length && flat[k].tone === "add") k++;
    const dels = flat.slice(i, j) as { tone: "del"; text: string }[], adds = flat.slice(j, k) as { tone: "add"; text: string }[];
    if (dels.length === 1 && adds.length === 1) out.push({ tone: "pair", del: dels[0].text, add: adds[0].text });
    else {
      // Each removed fact meets the first unused added line with the same fact; the rest keep their order after the pairs.
      const used = new Set<number>(), lone: DiffRow[] = [];
      for (const d of dels) {
        const f = factOf(d.text), n = f ? adds.findIndex((a, x) => !used.has(x) && factOf(a.text) === f) : -1;
        if (n >= 0) { used.add(n); out.push({ tone: "pair", del: d.text, add: adds[n].text }); } else lone.push(d);
      }
      out.push(...lone, ...adds.filter((_, x) => !used.has(x)));
    }
    i = k;
  }
  return out.some((r) => r.tone !== "ctx" && r.tone !== "gap") ? out : [];  // only blank lines changed: nothing to show
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

/** Whether a transcript line is the expert's own text, rendered as markdown; "prompt", "tool" and "refused" lines (and
 *  any other role) stay plain preformatted text. */
export const turnIsMarkdown = (role: string): boolean => role === "expert";

/** A transcript role as its tag ("prompt", "expert", "tool", "refused"; anything else as sent). */
export const roleTag = (r: string): string => r || "?";

/** Settings words that differ for an expert, whose skills are reference files it reads (not playbooks a task mounts)
 *  and which has no browser or routines: dialogs/BotSettings.tsx and SkillsSection.tsx. An ordinary bot's words are
 *  the ones those dialogs always had. */
export function settingsWords(kind: string | undefined): { instructionsPlaceholder: string; description: string } {
  if (kind === "expert") {
    return {
      instructionsPlaceholder: "e.g. “Answer questions about writing canvases; keep answers short.”",
      description: "How this expert answers, the model it runs on, the folders it may read and its skills (the reference files it reads).",
    };
  }
  return {
    instructionsPlaceholder: "e.g. “Browse LinkedIn for me. Answer in bullets. Never like or comment.”",
    description: `How this bot thinks, what it may do without asking, the services it may use, its browser, its playbooks, its routines, its memory${kind === "super" ? " and your phone" : ""}.`,
  };
}

export interface SkillFormWords { titlePlaceholder: string; triggerPlaceholder: string; triggerHint: string; bodyLabel: string; bodyPlaceholder: string }

/** The skill editor's words: a bot's playbook (steps a task's trigger word mounts) or an expert's reference file. */
export function skillFormWords(expert: boolean): SkillFormWords {
  return expert ? {
    titlePlaceholder: "e.g. Canvas folder layout",
    triggerPlaceholder: "canvas.toml, node files, folder layout",
    triggerHint: "Comma-separated; these words help the expert find this file when a question touches them.",
    bodyLabel: "Text",
    bodyPlaceholder: "What the expert should know about this, in markdown.",
  } : {
    titlePlaceholder: "e.g. LinkedIn feed summary",
    triggerPlaceholder: "linkedin feed, linkedin posts",
    triggerHint: "Comma-separated; any one appearing in a task mounts this playbook.",
    bodyLabel: "Steps",
    bodyPlaceholder: "1. Go to https://…\n2. Dismiss the cookie banner\n3. …",
  };
}
