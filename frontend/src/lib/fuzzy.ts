// Dependency-free, case-insensitive fuzzy subsequence matcher shared by the
// explorer and bookmark searches. Greedy left-to-right matching finds a
// subsequence whenever one exists (taking the earliest occurrence of each
// query char never blocks a later one); the score is a heuristic, not an
// optimum. Returns null when the query's chars don't all appear in order.
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

export function fuzzyMatch(query: string, text: string): FuzzyResult | null {
  if (query === "") return { score: 0, positions: [], longestRun: 0 };
  const q = query.toLowerCase();
  const t = text.toLowerCase();
  const positions: number[] = [];
  let qi = 0;
  let score = 0;
  let prev = -2; // index of the previously matched char, for the consecutive test
  let run = 0; // length of the current consecutive matched stretch
  let longestRun = 0;
  for (let ti = 0; ti < t.length && qi < q.length; ti++) {
    if (t[ti] !== q[qi]) continue;
    positions.push(ti);
    score += 1;
    run = ti === prev + 1 ? run + 1 : 1;
    if (run > longestRun) longestRun = run;
    if (ti === prev + 1) score += 3; // consecutive run
    if (isSegmentStart(text, ti)) score += 5; // landed on a word boundary
    prev = ti;
    qi++;
  }
  if (qi < q.length) return null; // ran out of text before matching every char
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
