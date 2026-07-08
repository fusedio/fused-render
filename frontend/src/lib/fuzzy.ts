// Dependency-free, case-insensitive fuzzy subsequence matcher shared by the
// explorer and bookmark searches. Greedy left-to-right matching finds a
// subsequence whenever one exists (taking the earliest occurrence of each
// query char never blocks a later one); the score is a heuristic, not an
// optimum. Returns null when the query's chars don't all appear in order.
export interface FuzzyResult {
  score: number;
  positions: number[]; // indices in `text` of the matched chars, ascending
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

export function fuzzyMatch(query: string, text: string): FuzzyResult | null {
  if (query === "") return { score: 0, positions: [] };
  const q = query.toLowerCase();
  const t = text.toLowerCase();
  const positions: number[] = [];
  let qi = 0;
  let score = 0;
  let prev = -2; // index of the previously matched char, for the consecutive test
  for (let ti = 0; ti < t.length && qi < q.length; ti++) {
    if (t[ti] !== q[qi]) continue;
    positions.push(ti);
    score += 1;
    if (ti === prev + 1) score += 3; // consecutive run
    if (isSegmentStart(text, ti)) score += 5; // landed on a word boundary
    prev = ti;
    qi++;
  }
  if (qi < q.length) return null; // ran out of text before matching every char
  return { score, positions };
}

// Plain case-insensitive substring matcher — the "fuzzy off" mode. Same
// result shape as fuzzyMatch so callers can swap matchers without branching:
// positions are the contiguous run covering the first occurrence of `query`
// in `text`. Score favors an earlier match and a match landing on a segment
// start, mirroring fuzzyMatch's weighting so the two scales feel consistent
// (each mode only ever ranks its own results, so exact cross-mode parity
// isn't required).
export function substringMatch(query: string, text: string): FuzzyResult | null {
  if (query === "") return { score: 0, positions: [] };
  const q = query.toLowerCase();
  const t = text.toLowerCase();
  const index = t.indexOf(q);
  if (index === -1) return null;
  const positions: number[] = [];
  for (let i = 0; i < q.length; i++) positions.push(index + i);
  let score = q.length * 4; // consecutive run: 1 (char) + 3 (consecutive) per char, as in fuzzyMatch
  if (isSegmentStart(text, index)) score += 5; // landed on a word boundary
  score -= index; // earlier position scores higher
  return { score, positions };
}

// Whether fuzzy (subsequence) matching is on; off falls back to
// substringMatch. Persisted so the choice survives a reload; both the
// explorer and sidebar searches read/write this single shared pref.
const FUZZY_PREF_KEY = "fused.searchFuzzy";
export const SEARCH_FUZZY_EVENT = "fused:searchFuzzy";

export function isFuzzyEnabled(): boolean {
  return localStorage.getItem(FUZZY_PREF_KEY) !== "false"; // default true
}

export function setFuzzyEnabled(value: boolean): void {
  localStorage.setItem(FUZZY_PREF_KEY, String(value));
  window.dispatchEvent(new Event(SEARCH_FUZZY_EVENT));
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
