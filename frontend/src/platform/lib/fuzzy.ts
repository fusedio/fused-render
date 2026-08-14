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
// over a 150k-entry corpus on every keystroke (listing/scan-job), so nothing
// here may become a dynamic-programming alignment.
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

export function fuzzyMatch(query: string, text: string): FuzzyResult | null {
  if (query === "") return { score: 0, positions: [], longestRun: 0 };
  const q = query.toLowerCase();
  const t = text.toLowerCase();
  const sub = t.indexOf(q);
  if (sub !== -1) {
    // The substring branch is untouched, and stays AHEAD of everything below.
    // `longestRun = q.length` is the maximum the subsequence branch can never
    // reach, and rankCompare orders on longestRun first — that is what
    // guarantees substring-over-fuzzy (listing/search.ts). Its span is the query
    // length by construction, so the bound cannot apply to it.
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
