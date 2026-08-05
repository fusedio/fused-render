// AI-assisted file search (pattern "query understanding"): one haiku call
// through /api/ai translates a natural-language query into a strict, tiny
// filter spec; the spec then runs SYSTEM-WIDE through /api/search/files
// (Spotlight on macOS, a bounded home walk elsewhere), and the hits are
// ranked client-side with the shared fuzzy matcher. The model never sees the
// filesystem and never returns anything executable — its output is data,
// validated field by field on BOTH sides of the wire (here at the parse
// boundary, and again in the server's _parse_spec), and a garbage reply
// degrades to a plain term search on the user's own words.
import { aiComplete, searchFiles, type SearchFileEntry } from "@platform/lib/api";
import { fuzzyMatch } from "@platform/lib/fuzzy";

export interface AiSearchSpec {
  // Terms to match against file names — only words likely to appear IN the
  // name. Concept words ("video", "spreadsheet") belong in extensions, not
  // here; the system prompt is explicit about the split.
  name_terms: string[];
  // Bare extensions (no dot), lowercase. Empty = any. Only files are ever
  // extension-filtered; a dir hit passes regardless.
  extensions: string[];
  kind: "file" | "dir" | "any";
  // Inclusive YYYY-MM-DD date ranges; either end may be open (null).
  // Modified = last content change; created = filesystem birth time.
  modified_after: string | null;
  modified_before: string | null;
  created_after: string | null;
  created_before: string | null;
  min_size_bytes: number | null;
  max_size_bytes: number | null;
  // Directory-name hints ("in my photos folder", "downloaded" → downloads) —
  // soft ranking boosts, never hard filters, because the model is guessing
  // at folder names it cannot see.
  path_hints: string[];
}

export interface AiSearchHit extends SearchFileEntry {
  score: number;
}

export interface AiSearchResult {
  hits: AiSearchHit[];
  truncated: boolean; // the engine hit its result cap
  usedFallback: boolean; // the model reply was unusable; plain term search ran
  engine: string; // "spotlight" | "walk" — the server says which ran
  spec: AiSearchSpec;
}

const MAX_HITS = 60;

// The date rides in the system prompt so relative phrases ("last week")
// resolve without the model guessing the current date.
export function aiSearchSystemPrompt(): string {
  return (
    "You translate a natural-language file-search query into a JSON filter " +
    "for a whole-computer file search. " +
    `Today's date is ${new Date().toISOString().slice(0, 10)}. ` +
    "Reply with ONLY a JSON object — no prose, no code fences — with exactly these keys:\n" +
    '{"name_terms": [words likely to appear IN the file\'s name],\n' +
    ' "extensions": [bare lowercase extensions like "csv", empty if any],\n' +
    ' "kind": "file" | "dir" | "any",\n' +
    ' "modified_after": "YYYY-MM-DD" or null,\n' +
    ' "modified_before": "YYYY-MM-DD" or null,\n' +
    ' "created_after": "YYYY-MM-DD" or null,\n' +
    ' "created_before": "YYYY-MM-DD" or null,\n' +
    ' "min_size_bytes": number or null,\n' +
    ' "max_size_bytes": number or null,\n' +
    ' "path_hints": [folder-name words the query implies, empty if none]}\n' +
    "Guidelines:\n" +
    "- name_terms is ONLY for words plausibly in the filename itself " +
    "(a project name, a topic, 'resume'). NEVER put file-type or media-type " +
    "words there — a video file is rarely named 'video'. Filler ('file', " +
    "'show', 'find', 'my', 'downloaded') never goes in name_terms.\n" +
    "- Map type words to extensions: video→mov,mp4,m4v,webm,avi,mkv; " +
    "photo/image/screenshot→png,jpg,jpeg,heic,gif; audio/song→mp3,m4a,wav,flac; " +
    "spreadsheet→csv,xlsx; notebook→ipynb; doc/document→pdf,docx,md,txt; " +
    "presentation→pptx,key.\n" +
    "- Dates are inclusive YYYY-MM-DD ranges computed from today's date; " +
    "leave an end null when the query only bounds one side. today→" +
    "modified_after today; yesterday→after yesterday, before yesterday; " +
    "'in June'→after 06-01, before 06-30; last week / recently→after " +
    "(today-7d); 'before March'→modified_before 02-28 only. Phrases about " +
    "when a file was made/added/downloaded ('created', 'made', 'saved', " +
    "'downloaded') use the created_* fields instead; when unsure, use " +
    "modified_*.\n" +
    "- Sizes: big/large→min_size_bytes 10000000; huge→100000000; " +
    "small/tiny→max_size_bytes 100000.\n" +
    "- 'folder'/'directory'→kind \"dir\".\n" +
    "- Location words go to path_hints: downloaded/downloads→downloads; " +
    "desktop→desktop; documents→documents; photos/pictures→pictures.\n" +
    "- When the query is only a name, everything except name_terms stays at " +
    "its empty/null default."
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
  // A real calendar date, not just the shape of one (the Date round-trip
  // rejects 2026-02-31); anything else from the model degrades to null.
  const dateStr = (v: unknown): string | null => {
    if (typeof v !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(v)) return null;
    const d = new Date(v + "T00:00:00Z");
    return isNaN(d.getTime()) || d.toISOString().slice(0, 10) !== v ? null : v;
  };
  const kind = o.kind === "file" || o.kind === "dir" ? o.kind : "any";
  return {
    name_terms: strings(o.name_terms).slice(0, 8),
    extensions: strings(o.extensions)
      .map((e) => e.toLowerCase().replace(/^\./, ""))
      .filter((e) => /^[a-z0-9]{1,12}$/.test(e))
      .slice(0, 8),
    kind,
    modified_after: dateStr(o.modified_after),
    modified_before: dateStr(o.modified_before),
    created_after: dateStr(o.created_after),
    created_before: dateStr(o.created_before),
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
    modified_after: null,
    modified_before: null,
    created_after: null,
    created_before: null,
    min_size_bytes: null,
    max_size_bytes: null,
    path_hints: [],
  };
}

// "2026-06-01 → 2026-06-30", "since 2026-08-04", "until 2026-03-01".
function fmtRange(after: string | null, before: string | null): string {
  if (after !== null && before !== null)
    return after === before ? `on ${after}` : `${after} → ${before}`;
  return after !== null ? `since ${after}` : `until ${before}`;
}

// Human-readable echo of what the model understood, shown next to results so
// a wrong interpretation is visible instead of silently shaping the list.
export function describeSpec(spec: AiSearchSpec): string {
  const parts: string[] = [];
  if (spec.name_terms.length) parts.push(`“${spec.name_terms.join(" ")}”`);
  if (spec.extensions.length) parts.push("." + spec.extensions.join(" ."));
  if (spec.kind !== "any") parts.push(spec.kind === "dir" ? "folders" : "files");
  if (spec.modified_after !== null || spec.modified_before !== null)
    parts.push(`modified ${fmtRange(spec.modified_after, spec.modified_before)}`);
  if (spec.created_after !== null || spec.created_before !== null)
    parts.push(`created ${fmtRange(spec.created_after, spec.created_before)}`);
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

// Whether the spec constrains anything besides name terms. When it does,
// name terms turn SOFT (boost-only): "video downloaded today" already pins
// extension+date, and a hard name filter would reject IMG_1234.mov for not
// containing "video".
export function hasNonNameFilters(spec: AiSearchSpec): boolean {
  return (
    spec.extensions.length > 0 ||
    spec.modified_after !== null ||
    spec.modified_before !== null ||
    spec.created_after !== null ||
    spec.created_before !== null ||
    spec.min_size_bytes !== null ||
    spec.max_size_bytes !== null
  );
}

// Rank the engine's hits: fuzzy name-term score + path-hint boost, recency
// tie-break (and the whole order when the spec has no name terms). The
// engine already applied the HARD filters (ext/kind/date/size); name terms
// are re-scored here because Spotlight matched them with a dumb *term* glob
// and can't rank, and the walk fallback didn't match them at all.
export function rankHits(
  entries: SearchFileEntry[],
  spec: AiSearchSpec,
  home: string,
): AiSearchHit[] {
  const soft = hasNonNameFilters(spec);
  const hits: AiSearchHit[] = [];
  for (const e of entries) {
    // Score against the home-relative path — rooting at "/" would let
    // /Users/<name> segments match name terms.
    const rel = e.path.startsWith(home + "/") ? e.path.slice(home.length + 1) : e.path;
    let score = 0;
    if (spec.name_terms.length) {
      let matched = 0;
      for (const term of spec.name_terms) {
        const m = fuzzyMatch(term, rel);
        // A subsequence match with no real run is noise ("csv" matching
        // scattered letters of a long path); demand some substance.
        if (m && (m.longestRun >= Math.min(3, term.length) || term.length <= 2)) {
          matched++;
          score += m.score;
        }
      }
      if (matched === 0 && !soft) continue;
    }
    const dirPart = rel.slice(0, rel.lastIndexOf("/") + 1).toLowerCase();
    for (const hint of spec.path_hints) {
      if (dirPart.includes(hint.toLowerCase())) score += 10;
    }
    hits.push({ ...e, score });
  }
  hits.sort((a, b) => b.score - a.score || (b.mtime ?? 0) - (a.mtime ?? 0));
  return hits.slice(0, MAX_HITS);
}

// The engine treats name terms as a hard glob, so a spec that ALSO carries
// real filters retries without the terms when the first pass comes up empty
// — same rationale as the soft-term ranking above.
async function queryEngine(spec: AiSearchSpec, signal?: AbortSignal) {
  const first = await searchFiles(spec, signal);
  if (first.entries.length || !spec.name_terms.length || !hasNonNameFilters(spec))
    return first;
  return searchFiles({ ...spec, name_terms: [] }, signal);
}

// The full pipeline: model → spec → system-wide engine → rank.
export async function runAiSearch(
  home: string,
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
  const res = await queryEngine(spec, signal);
  return {
    hits: rankHits(res.entries, spec, home),
    truncated: res.truncated,
    usedFallback,
    engine: res.engine,
    spec,
  };
}
