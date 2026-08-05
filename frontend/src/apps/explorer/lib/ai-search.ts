// AI-assisted file search (pattern "query understanding"): one haiku call
// through /api/ai translates a natural-language query into a strict, tiny
// filter spec; the spec then runs against the SAME bounded /api/fs/walk the
// listing search uses, ranked client-side with the shared fuzzy matcher. The
// model never sees the filesystem and never returns anything executable —
// its output is data, validated field by field, and a garbage reply degrades
// to a plain fuzzy search on the user's own words.
import { aiComplete, walkDirStream, type WalkEntry } from "@platform/lib/api";
import { fuzzyMatch } from "@platform/lib/fuzzy";

export interface AiSearchSpec {
  // Substrings/terms to fuzzy-match against the entry's relative path. Empty
  // means "no name constraint" (a pure date/type query like "files from
  // today"), in which case results order by recency instead of match score.
  name_terms: string[];
  // Bare extensions (no dot), lowercase. Empty = any. Only files are ever
  // extension-filtered; a dir hit passes regardless.
  extensions: string[];
  kind: "file" | "dir" | "any";
  modified_within_days: number | null;
  min_size_bytes: number | null;
  max_size_bytes: number | null;
  // Directory-name hints ("in my photos folder") — soft boosts, never hard
  // filters, because the model is guessing at folder names it cannot see.
  path_hints: string[];
}

export interface AiSearchHit {
  rel: string;
  is_dir: boolean;
  size: number | null;
  mtime: number | null;
  score: number;
}

export interface AiSearchResult {
  hits: AiSearchHit[];
  truncated: boolean; // the walk hit its server-side entry cap
  usedFallback: boolean; // the model reply was unusable; plain fuzzy ran
  spec: AiSearchSpec;
}

const MAX_HITS = 60;

// The date rides in the system prompt so relative phrases ("last week")
// resolve without the model guessing the current date.
export function aiSearchSystemPrompt(): string {
  return (
    "You translate a natural-language file-search query into a JSON filter. " +
    `Today's date is ${new Date().toISOString().slice(0, 10)}. ` +
    "Reply with ONLY a JSON object — no prose, no code fences — with exactly these keys:\n" +
    '{"name_terms": [strings to match in file names/paths],\n' +
    ' "extensions": [bare lowercase extensions like "csv", empty if any],\n' +
    ' "kind": "file" | "dir" | "any",\n' +
    ' "modified_within_days": number or null,\n' +
    ' "min_size_bytes": number or null,\n' +
    ' "max_size_bytes": number or null,\n' +
    ' "path_hints": [folder-name words the query implies, empty if none]}\n' +
    "Guidelines: keep name_terms to the distinctive content words of the query " +
    "(never filler like 'file', 'show', 'find', 'my'); map format words to " +
    "extensions (spreadsheet→csv,xlsx; photo/screenshot→png,jpg,jpeg; " +
    "notebook→ipynb; doc→md,pdf,docx); 'big'/'large'→min_size_bytes 10000000; " +
    "'folder'/'directory'→kind dir. When the query is only a name, everything " +
    "except name_terms stays at its empty/null default."
  );
}

// Parse + validate the model's reply into a spec, or null when it isn't one.
// Every field is coerced defensively: this is the trust boundary between
// model output and code that acts on it.
export function parseAiSearchSpec(text: string): AiSearchSpec | null {
  // Models fence JSON despite instructions often enough to be worth peeling.
  const peeled = text.trim().replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/, "");
  let raw: unknown;
  try {
    raw = JSON.parse(peeled);
  } catch {
    return null;
  }
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return null;
  const o = raw as Record<string, unknown>;
  const strings = (v: unknown): string[] =>
    Array.isArray(v)
      ? v.filter((s): s is string => typeof s === "string" && s.trim() !== "").map((s) => s.trim())
      : [];
  const posNum = (v: unknown): number | null =>
    typeof v === "number" && isFinite(v) && v > 0 ? v : null;
  const kind = o.kind === "file" || o.kind === "dir" ? o.kind : "any";
  return {
    name_terms: strings(o.name_terms).slice(0, 8),
    extensions: strings(o.extensions)
      .map((e) => e.toLowerCase().replace(/^\./, ""))
      .filter((e) => /^[a-z0-9]{1,12}$/.test(e))
      .slice(0, 8),
    kind,
    modified_within_days: posNum(o.modified_within_days),
    min_size_bytes: posNum(o.min_size_bytes),
    max_size_bytes: posNum(o.max_size_bytes),
    path_hints: strings(o.path_hints).slice(0, 4),
  };
}

// When the relay is down or replied garbage: the user's own words become
// name_terms, so the search still does something sensible.
export function fallbackSpec(query: string): AiSearchSpec {
  return {
    name_terms: query.split(/\s+/).filter(Boolean).slice(0, 8),
    extensions: [],
    kind: "any",
    modified_within_days: null,
    min_size_bytes: null,
    max_size_bytes: null,
    path_hints: [],
  };
}

// Human-readable echo of what the model understood, shown next to results so
// a wrong interpretation is visible instead of silently shaping the list.
export function describeSpec(spec: AiSearchSpec): string {
  const parts: string[] = [];
  if (spec.name_terms.length) parts.push(`“${spec.name_terms.join(" ")}”`);
  if (spec.extensions.length) parts.push("." + spec.extensions.join(" ."));
  if (spec.kind !== "any") parts.push(spec.kind === "dir" ? "folders" : "files");
  if (spec.modified_within_days !== null)
    parts.push(`modified ≤${spec.modified_within_days}d`);
  if (spec.min_size_bytes !== null) parts.push(`≥${fmtBytes(spec.min_size_bytes)}`);
  if (spec.max_size_bytes !== null) parts.push(`≤${fmtBytes(spec.max_size_bytes)}`);
  if (spec.path_hints.length) parts.push("near " + spec.path_hints.join(", "));
  return parts.join(" · ");
}

function fmtBytes(n: number): string {
  if (n >= 1e9) return `${Math.round(n / 1e8) / 10}GB`;
  if (n >= 1e6) return `${Math.round(n / 1e5) / 10}MB`;
  if (n >= 1e3) return `${Math.round(n / 100) / 10}KB`;
  return `${n}B`;
}

// Hard filters first (cheap, prune early), then fuzzy score. With name_terms
// present at least ONE term must match — requiring all would punish the
// model's habit of listing synonyms ("resume", "cv") as separate terms.
export function scoreEntry(e: WalkEntry, spec: AiSearchSpec, nowS: number): number | null {
  if (spec.kind === "file" && e.is_dir) return null;
  if (spec.kind === "dir" && !e.is_dir) return null;
  if (!e.is_dir && spec.extensions.length) {
    const dot = e.rel.lastIndexOf(".");
    const ext = dot === -1 ? "" : e.rel.slice(dot + 1).toLowerCase();
    if (!spec.extensions.includes(ext)) return null;
  }
  if (spec.modified_within_days !== null) {
    if (e.mtime === null || e.mtime < nowS - spec.modified_within_days * 86400) return null;
  }
  if (!e.is_dir && spec.min_size_bytes !== null && (e.size ?? 0) < spec.min_size_bytes)
    return null;
  if (!e.is_dir && spec.max_size_bytes !== null && (e.size ?? 0) > spec.max_size_bytes)
    return null;
  let score = 0;
  if (spec.name_terms.length) {
    let matched = 0;
    for (const term of spec.name_terms) {
      const m = fuzzyMatch(term, e.rel);
      // A subsequence match with no real run is noise ("csv" matching
      // scattered letters of a long path); demand some substance.
      if (m && (m.longestRun >= Math.min(3, term.length) || term.length <= 2)) {
        matched++;
        score += m.score;
      }
    }
    if (matched === 0) return null;
  }
  const dirPart = e.rel.slice(0, e.rel.lastIndexOf("/") + 1).toLowerCase();
  for (const hint of spec.path_hints) {
    if (dirPart.includes(hint.toLowerCase())) score += 10;
  }
  return score;
}

export function rankEntries(
  entries: WalkEntry[],
  spec: AiSearchSpec,
  nowS: number = Date.now() / 1000,
): AiSearchHit[] {
  const hits: AiSearchHit[] = [];
  for (const e of entries) {
    const score = scoreEntry(e, spec, nowS);
    if (score !== null) hits.push({ ...e, score });
  }
  // Tie-break (and the whole order for term-less specs) is recency.
  hits.sort((a, b) => b.score - a.score || (b.mtime ?? 0) - (a.mtime ?? 0));
  return hits.slice(0, MAX_HITS);
}

// The full pipeline: model → spec → bounded walk → rank. The walk streams,
// but ranking waits for the end — a homepage search is one-shot, not
// search-as-you-type, so there is no partial-paint pressure here.
export async function runAiSearch(
  root: string,
  query: string,
  signal?: AbortSignal,
): Promise<AiSearchResult> {
  let spec: AiSearchSpec | null = null;
  try {
    spec = parseAiSearchSpec(await aiComplete(query, aiSearchSystemPrompt()));
  } catch {
    // relay down / claude missing — fall through to the fallback spec
  }
  const usedFallback = spec === null;
  if (spec === null) spec = fallbackSpec(query);
  if (signal?.aborted) throw new DOMException("aborted", "AbortError");
  const entries: WalkEntry[] = [];
  const end = await walkDirStream(root, {
    signal,
    onBatch: (batch) => entries.push(...batch),
  });
  return { hits: rankEntries(entries, spec), truncated: end.truncated, usedFallback, spec };
}
