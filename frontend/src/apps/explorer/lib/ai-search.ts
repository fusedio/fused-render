// AI-assisted file search (pattern "query understanding"): one haiku call
// through /api/ai translates a natural-language query into a strict, tiny
// filter spec; the spec then runs through /api/search/files, which executes it
// as one SQL query against the app's own file index (the only engine — it covers
// the indexed roots, home by default, and reports a missing index as an error
// rather than as an empty disk), and the hits are ranked client-side with the
// shared fuzzy matcher. The model never sees the filesystem and never returns
// anything executable — its output is data, validated field by field on BOTH
// sides of the wire (here at the parse boundary, and again in the server's
// _parse_spec).
//
// This is a DELIBERATE action, not the default path: the explorer's home page
// answers plain filename queries instantly from the file index, and AI search
// is one row the user has to pick (see FilesHome). Everything that existed to
// make an AI-first box tolerable is therefore gone — no keyword fallback spec
// when the model replies with garbage, no name-terms-stripped retry when the
// first engine query comes up empty (the softening that retry existed for is
// decided UP FRONT instead — see engineSpec). One model call, one engine query, and a
// failure is REPORTED: the index results the user was already looking at are
// the fallback, and silently answering a different question (a keyword search
// on their raw words) would be worse than saying so.
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
  // Inclusive YYYY-MM-DD range over the last content change; either end may be
  // open (null). There is no creation-date filter: the index records mtime and
  // no birth time, so the server refuses one outright rather than silently
  // answering a different question (see search.py).
  modified_after: string | null;
  modified_before: string | null;
  min_size_bytes: number | null;
  max_size_bytes: number | null;
  // Directory-name hints ("in my photos folder", "downloaded" → downloads).
  // A real engine constraint: each hint must match a path SEGMENT, OR'd across
  // hints and ANDed with the rest of the spec (see search.py). It used to be
  // documented as a client-side ranking boost, which made "Downloads this week"
  // execute as a date filter over the whole index and truncate to the newest
  // rows ANYWHERE — a boost cannot recover rows the SQL already dropped.
  path_hints: string[];
}

export interface AiSearchHit extends SearchFileEntry {
  score: number;
}

export interface AiSearchResult {
  hits: AiSearchHit[];
  truncated: boolean; // the engine hit its result cap
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
    ' "min_size_bytes": number or null,\n' +
    ' "max_size_bytes": number or null,\n' +
    ' "path_hints": [folder-name words the query implies, empty if none]}\n' +
    "Guidelines:\n" +
    "- At least one of name_terms, extensions, path_hints, or a date/size " +
    "bound MUST be set: kind alone (\"folders\") still matches half the disk, " +
    "so a reply carrying only that has no answer to give.\n" +
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
    "(today-7d); 'before March'→modified_before 02-28 only. There is no " +
    "creation-date field, so phrases about when a file was made, added, saved " +
    "or downloaded use the modified_* range too.\n" +
    "- Sizes: big/large→min_size_bytes 10000000; huge→100000000; " +
    "small/tiny→max_size_bytes 100000.\n" +
    "- 'folder'/'directory'→kind \"dir\".\n" +
    "- Location words go to path_hints: downloaded/downloads→downloads; " +
    "desktop→desktop; documents→documents; photos/pictures→pictures. These " +
    "RESTRICT the search to folders with that exact name, so only name a place " +
    "the query actually asks for — a guess excludes everywhere else."
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
    min_size_bytes: posNum(o.min_size_bytes),
    max_size_bytes: posNum(o.max_size_bytes),
    path_hints: strings(o.path_hints).slice(0, 4),
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
  if (spec.min_size_bytes !== null) parts.push(`≥${fmtBytes(spec.min_size_bytes)}`);
  if (spec.max_size_bytes !== null) parts.push(`≤${fmtBytes(spec.max_size_bytes)}`);
  // "in", not "near": the engine restricts to these folders (see path_hints).
  if (spec.path_hints.length) parts.push("in " + spec.path_hints.join(", "));
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
    spec.min_size_bytes !== null ||
    spec.max_size_bytes !== null
  );
}

// Whether the spec gives the ENGINE (server) anything to narrow on. Every
// field but `kind` counts — path_hints included, since it is a path-segment
// WHERE clause (search.py), so "in downloads" is answerable. Only a spec that
// narrows nothing at all is refused here, one round trip before the endpoint
// would refuse it in its own words.
function hasEngineNarrowing(spec: AiSearchSpec): boolean {
  return spec.name_terms.length > 0 || spec.path_hints.length > 0 || hasNonNameFilters(spec);
}

/**
 * The spec as the ENGINE should execute it: name terms dropped when the spec
 * narrows on anything else.
 *
 * The server ANDs name_terms in as an OR of ILIKEs, which is a hard filter on
 * the file's name — and for "videos I downloaded last week" that is the wrong
 * question: the query is already pinned by extension and date, and IMG_1234.mov
 * would be excluded for never saying "video". So when there is other real
 * narrowing the terms become boost-only and rankHits alone uses them (its soft
 * branch, previously unreachable in production). When they are the ONLY
 * narrowing they must stay a predicate — otherwise the query matches the index.
 *
 * This is deliberately not the deleted name-terms-stripped RETRY: the decision
 * is made before the single request, so it is still one model call and one
 * engine query.
 */
export function engineSpec(spec: AiSearchSpec): AiSearchSpec {
  return hasNonNameFilters(spec) ? { ...spec, name_terms: [] } : spec;
}

// Rank the engine's hits: fuzzy name-term score, recency tie-break (and the
// whole order when the spec has no name terms). The engine applied the hard
// filters it was sent (ext/kind/date/size/path); name terms are scored here
// because the engine either matched them with a dumb case-insensitive substring
// or — under engineSpec's softening — never saw them at all, and cannot rank
// either way. path_hints gets no boost: every returned row already matches one.
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
    hits.push({ ...e, score });
  }
  hits.sort((a, b) => b.score - a.score || (b.mtime ?? 0) - (a.mtime ?? 0));
  return hits.slice(0, MAX_HITS);
}

// The full pipeline: model → spec → the index engine → rank. One round trip
// each, and every failure throws for the caller to show — see the module
// header on why there is nothing to degrade to here.
export async function runAiSearch(
  home: string,
  query: string,
  signal?: AbortSignal,
): Promise<AiSearchResult> {
  // A relay failure (no claude, model error) propagates as-is: its message is
  // already the most specific thing anyone can say about it.
  const spec = parseAiSearchSpec(await aiComplete(query, aiSearchSystemPrompt()));
  if (spec === null)
    throw new Error(
      "AI search could not read the model's reply as a filter. " +
        "Try rephrasing, or search by name.",
    );
  if (!hasEngineNarrowing(spec))
    throw new Error(
      "AI search found nothing to narrow by — add a name, a place, a file " +
        "type, or a date.",
    );
  if (signal?.aborted) throw new DOMException("aborted", "AbortError");
  // The engine runs the softened spec; the ECHO shows what the model
  // understood, so a wrong interpretation is still visible.
  const res = await searchFiles(engineSpec(spec), signal);
  return { hits: rankHits(res.entries, spec, home), truncated: res.truncated, spec };
}
