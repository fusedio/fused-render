// Dependency-free, case-insensitive fuzzy subsequence matcher shared by the
// explorer and bookmark searches. Returns null when the query's chars don't all
// appear in order, or when the alignment is too spread out to mean anything.
//
// TWO passes, not one, and the second is what makes the result usable:
//
//   1. FORWARD, greedy-earliest. This decides only two things — whether a
//      subsequence exists at all (taking the earliest occurrence of each query
//      char never blocks a later one) and the earliest index the match can END
//      at.
//   2. BACKWARD from that end, binding each query char as LATE as possible.
//
// Pass 1 alone was the bug. Query `index.md` over
// `/Users/iamsdas/…/index/specs/index-store.md` bound `i` to `iamsdas`, `n` to
// `render` and so on, smearing eight chars across the whole path while a
// near-perfect match sat in the last segment — a bad score, a worse
// `longestRun`, and a highlight of scattered letters. Packing leftwards from a
// fixed end snaps the whole thing onto `index-store.md`. This is the standard
// fzf-style tighten; it is still a heuristic, not an optimum (the end stays the
// earliest feasible one), but it is the alignment a human reading the path
// would pick. `positions`, `score` and `longestRun` all come from pass 2.
//
// Then the SPAN bound: a match whose tightened alignment still stretches further
// than `maxSpan(query.length)` is refused. Bounding the total span rather than
// each gap is deliberate — a good match is often two tight halves either side of
// one long gap (`index` … `.md` across a folder name), so a per-gap cap has to
// be loose enough for that, and once it is, it no longer catches the smear.
//
// Both passes are O(text length) and allocate one positions array. This runs
// over every ranked hit the server returns on every keystroke
// (listing/ranked-hits), so nothing here may become a dynamic-programming
// alignment.
export interface FuzzyResult {
  score: number;
  positions: number[]; // indices in `text` of the matched chars, ascending
  longestRun: number; // length of the longest consecutive matched stretch
}

// Chars that open a new "segment" in a path/name; a match right after one of
// these reads as the start of a word and scores higher.
const SEPARATORS = new Set(["/", ".", "-", "_", " "]);

function isUpper(ch: string): boolean {
  return ch >= "A" && ch <= "Z";
}

// Segment start = index 0, the char after a separator, or a camelCase hump
// (a non-upper followed by an upper). Uses the original-case text so the
// camelCase test survives the lowercasing done for matching.
function isSegmentStart(text: string, i: number): boolean {
  if (i === 0) return true;
  const prev = text[i - 1];
  if (SEPARATORS.has(prev)) return true;
  return isUpper(text[i]) && !isUpper(prev);
}

/**
 * How far a `queryLength`-char match may stretch, first matched char to last.
 *
 * Tuned against this repo's real paths, not derived: for the queries a person
 * actually types the tightened span sits at the query length plus a handful
 * (`indexstore` → 11 over 10, `explorersearch` → 15-30 over 14, `fusedindex` →
 * 18 over 10), while the smears that prompted this are 40-78. `3n + 8` sits
 * between the two everywhere it was measured — it keeps every multi-segment
 * query in fuzzy.test.ts and refuses `index.md` against
 * `docs/LINUX_DESKTOP_SPEC.md`. The `+ 8` is what keeps very short queries
 * usable, where a proportional bound alone would be a couple of characters.
 *
 * NAMED COST: a very short query used as word initials over a long prose string
 * — `zmp` for the bookmark titled "Zarr v3 multiscale pyramid budget notes",
 * span 20 against a bound of 17 — stops matching. The obvious fix, an extra
 * allowance per matched char that lands on a segment start, was measured and is
 * WORSE: at `+4` per start it rescues that case and re-admits `index.md`
 * against `/Users/…/docs/EXPORT.md` (span 40, four segment starts, bound 48),
 * which is the very smear this exists to refuse. A short query over prose is
 * low-signal either way, and one more character typed fixes it; a whole-path
 * smear ranked among real hits is what the user actually reported.
 */
export function maxSpan(queryLength: number): number {
  return queryLength * 3 + 8;
}

/**
 * Case-insensitive substring match: `query` found verbatim and contiguous in
 * `text`, or null. The same test `fused_render/index/query.py`'s `_rank_sql`
 * filters on server-side (`WHERE lower(rel) LIKE '%q%'`) — home-search.ts's
 * `narrowAnswer` and `answerFrom`, and listing/ranked-hits.ts's `hitsFromRank`,
 * all call rows that already passed that server filter, so this is the exact
 * test they need, not `fuzzyMatch`'s strictly looser subsequence one.
 *
 * Extracted out of `fuzzyMatch`'s own substring fast path (below) rather than
 * reimplemented: this IS that branch, unchanged, just independently callable.
 * `query === ""` matches at position 0 with no positions, same as
 * `String.prototype.indexOf("")` always doing so — no special case needed.
 */
export function substringMatch(query: string, text: string): FuzzyResult | null {
  const q = query.toLowerCase();
  const t = text.toLowerCase();
  const sub = t.indexOf(q);
  if (sub === -1) return null;
  const positions: number[] = [];
  let score = 0;
  for (let ti = sub; ti < sub + q.length; ti++) {
    positions.push(ti);
    score += 1;
    if (ti > sub) score += 3; // consecutive run
    if (isSegmentStart(text, ti)) score += 5; // landed on a word boundary
  }
  return { score, positions, longestRun: q.length };
}

export function fuzzyMatch(query: string, text: string): FuzzyResult | null {
  // The substring branch stays AHEAD of everything below. `longestRun =
  // q.length` is the maximum the subsequence branch can never reach, and
  // rankCompare orders on longestRun first — that is what guarantees
  // substring-over-fuzzy (listing/search.ts). Its span is the query length by
  // construction, so the bound cannot apply to it.
  const sub = substringMatch(query, text);
  if (sub) return sub;
  const q = query.toLowerCase();
  const t = text.toLowerCase();
  // Pass 1: does a subsequence exist, and where is the earliest it can end?
  let qi = 0;
  let end = -1;
  for (let ti = 0; ti < t.length && qi < q.length; ti++) {
    if (t[ti] === q[qi]) {
      end = ti;
      qi++;
    }
  }
  if (qi < q.length) return null; // ran out of text before matching every char
  // Pass 2: the same match, packed as far right as `end` allows. Guaranteed to
  // complete — pass 1's alignment is itself a witness that ends at `end`, and
  // binding later can only ever be easier.
  const positions = new Array<number>(q.length);
  let qj = q.length - 1;
  for (let ti = end; ti >= 0 && qj >= 0; ti--) {
    if (t[ti] === q[qj]) positions[qj--] = ti;
  }
  if (end - positions[0] + 1 > maxSpan(q.length)) return null;
  // Scored over the TIGHTENED positions. Scoring pass 1's would have judged a
  // match nobody is going to see.
  let score = 0;
  let run = 0;
  let longestRun = 0;
  let prev = -2;
  for (const ti of positions) {
    score += 1;
    run = ti === prev + 1 ? run + 1 : 1;
    if (run > longestRun) longestRun = run;
    if (ti === prev + 1) score += 3; // consecutive run
    if (isSegmentStart(text, ti)) score += 5; // landed on a word boundary
    prev = ti;
  }
  return { score, positions, longestRun };
}

// A glob hit is not promised to be a subsequence or even a substring of the
// query text (`*.csv` matching `report.csv` has no literal "*.csv" anywhere
// in `report.csv`), so `fuzzyMatch`/`substringMatch` cannot answer what to
// highlight for one. This instead locates each LITERAL piece of the glob
// PATTERN (not the raw query — see `globMatch`'s own docstring) within
// `text` and marks only those, leaving the wildcard-filled gaps between them
// unmarked — SPEC-search-space-wildcard.md §4: "mark each literal piece
// separately" (`hello world` against `my_hello_big_world.py` marks `hello`
// and `world`, not the `_big_` between them).
//
// Mirrors `_glob_to_regex` (fused_render/index/query.py) token for token —
// `**/ ` is zero-or-more whole segments, a mid-pattern `**` spans
// separators unrestricted, a lone `*` is confined to one segment, and
// `?`/`[`/`]` are literal characters, same as every other char — except
// each literal RUN is wrapped in its own capturing group instead of being
// escaped inline, so the match's own capture indices (the `d` regex flag,
// `RegExpExecArray.indices`) give this the position of each piece directly,
// with no separate scan needed.
function escapeLiteral(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// The regex `d` flag (`hasIndices`, ES2022) is unsupported on WebKit before
// 16.4 — NOT a feature `exec` silently degrades on, but one `new RegExp`
// itself THROWS a `SyntaxError` for, at construction time. Every glob-mode
// hit on an affected browser would have thrown before it ever got the
// chance to render, breaking the whole listing rather than just this one
// highlight (code review finding). Probed once, lazily, and memoized: the
// probe itself is exactly the failure mode being guarded against, so it
// only ever needs to run once per page load, not once per query/hit.
let supportsIndicesFlagCache: boolean | undefined;
function supportsIndicesFlag(): boolean {
  if (supportsIndicesFlagCache === undefined) {
    try {
      new RegExp("", "d");
      supportsIndicesFlagCache = true;
    } catch {
      supportsIndicesFlagCache = false;
    }
  }
  return supportsIndicesFlagCache;
}

// Test-only: every real engine this repo runs against supports the "d"
// flag, so the WebKit < 16.4 fallback branch in `globMatch` has no other
// way to be exercised — this lets fuzzy.test.ts force the cache to either
// value (and clear it back to `undefined` afterward) without reaching for
// a global `RegExp` monkeypatch, which would also break every OTHER regex
// construction in this module, not just the probe.
export function __setSupportsIndicesFlagForTest(value: boolean | undefined): void {
  supportsIndicesFlagCache = value;
}

function globToHighlightRegex(pattern: string): RegExp {
  let out = "";
  let i = 0;
  const n = pattern.length;
  while (i < n) {
    if (pattern.startsWith("**/", i)) {
      out += "(?:[^/]*/)*";
      i += 3;
    } else if (pattern.startsWith("**", i)) {
      out += ".*";
      i += 2;
    } else if (pattern[i] === "*") {
      out += "[^/]*";
      i += 1;
    } else {
      let lit = "";
      while (i < n && pattern[i] !== "*") {
        lit += pattern[i];
        i++;
      }
      // A literal run immediately followed by the whole-segment "**/"
      // token ends in the "/" that INTRODUCES that token (the pattern
      // text is literally "dir/**/…") — that slash is the path separator
      // the pattern author typed between segments, not part of the
      // segment name itself, so it stays out of the capture: the spec's
      // own example marks `[src]/lib/util[.ts]`, not `[src/]lib/...`.
      let trailingSlash = "";
      if (lit.endsWith("/") && pattern.startsWith("**/", i)) {
        trailingSlash = "/";
        lit = lit.slice(0, -1);
      }
      if (lit) out += "(" + escapeLiteral(lit) + ")";
      out += escapeLiteral(trailingSlash);
    }
  }
  // The "d" flag is add-on metadata (`m.indices`), never a change to WHAT
  // matches — omitting it on an unsupporting engine still produces the same
  // capture groups, just without their positions attached; `globMatch`
  // below reconstructs those positions itself in that case.
  return new RegExp("^" + out + "$", supportsIndicesFlag() ? "d" : "");
}

// `RegExpExecArray` in this repo's ES2020 lib target has no `indices`
// member (that is an ES2022 addition) — asserted locally rather than
// bumping the project-wide lib target for one call site.
type IndicesArray = Array<[number, number] | undefined>;

/**
 * Highlight positions for a GLOB-mode hit: `pattern` is the server's
 * resolved pattern (`IndexRankResult.pattern`), already base-peeled and
 * whitespace-expanded — NOT the raw string the user typed, and not
 * `resolve_query`'s `**\/` prefix redone client-side (the pattern already
 * carries it). `text` is the hit's `rel`, exactly as `hitsFromRank`/
 * `answerFrom` already have it.
 *
 * Returns null when `text` does not actually full-match `pattern` — should
 * not happen for a hit the server already matched, but this does not
 * assume its caller only ever calls it with one that does.
 *
 * `score`/`longestRun` are 0: glob mode has no scoring (SPEC-search-space-
 * wildcard.md: "do not add scoring to glob mode") and nothing here reads
 * either field for a glob hit — only `positions` is used, by
 * `highlightSegments`.
 */
export function globMatch(pattern: string, text: string): FuzzyResult | null {
  const regex = globToHighlightRegex(pattern.toLowerCase());
  const lowerText = text.toLowerCase();
  const m = regex.exec(lowerText) as
    | (RegExpExecArray & { indices?: IndicesArray })
    | null;
  if (!m) return null;
  if (m.indices) {
    const positions: number[] = [];
    for (let g = 1; g < m.indices.length; g++) {
      const range = m.indices[g];
      if (!range) continue;
      for (let p = range[0]; p < range[1]; p++) positions.push(p);
    }
    return { score: 0, positions, longestRun: 0 };
  }
  // No "d" flag support (WebKit < 16.4) — `m.indices` doesn't exist, but the
  // captured substrings themselves (`m[1]`, `m[2]`, …) still do; `exec`
  // never needed the flag for those. Each capture is a LITERAL run of the
  // pattern, and the regex is a full match (`^...$`) against `lowerText`,
  // so the captures occur in the same left-to-right order they matched in —
  // a sequential `indexOf`, each search starting where the previous capture
  // left off, finds the same runs the "d" flag's own indices would have
  // pointed at (only ambiguous when a literal could itself recur inside a
  // wildcard's own span right before it, an existing edge case the "d" path
  // is equally exposed to via the regex engine's own backtracking choice).
  const positions: number[] = [];
  let cursor = 0;
  for (let g = 1; g < m.length; g++) {
    const lit = m[g];
    if (!lit) continue;
    const start = lowerText.indexOf(lit, cursor);
    if (start === -1) continue;
    for (let p = start; p < start + lit.length; p++) positions.push(p);
    cursor = start + lit.length;
  }
  return { score: 0, positions, longestRun: 0 };
}

export interface HighlightSegment {
  text: string;
  match: boolean;
}

// Split `text` into alternating matched / unmatched runs for highlight
// rendering. Positions are the ascending indices returned by fuzzyMatch.
export function highlightSegments(text: string, positions: number[]): HighlightSegment[] {
  if (!positions.length) return text ? [{ text, match: false }] : [];
  const marked = new Set(positions);
  const segments: HighlightSegment[] = [];
  let run = "";
  let runMatch = marked.has(0);
  for (let i = 0; i < text.length; i++) {
    const m = marked.has(i);
    if (m === runMatch) {
      run += text[i];
    } else {
      segments.push({ text: run, match: runMatch });
      run = text[i];
      runMatch = m;
    }
  }
  segments.push({ text: run, match: runMatch });
  return segments;
}
