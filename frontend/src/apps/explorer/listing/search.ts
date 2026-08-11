// Fuzzy scoring and ranking for the in-folder search (over the streamed walk).
import type { WalkEntry } from "@platform/lib/api";
import { fuzzyMatch } from "@platform/lib/fuzzy";
import type { SearchHit } from "@apps/explorer/listing/types";

// A dot-leading query segment is explicit intent to SEE hidden entries.
// The walk itself always includes hidden entries (one dataset — the server
// prunes the actually-heavy machine trees like .git/node_modules, so hidden
// files are cheap to carry); this only gates whether dot-entries are shown.
// That makes ".py" work as an extension search (dotfiles like .pylintrc may
// match too — fine, they're real matches) without a second walk, and "env"
// deliberately not surface ".env".
export function queryWantsHidden(rawQuery: string): boolean {
  const q = rawQuery.trim();
  return q.startsWith(".") || q.includes("/.");
}

// An entry is hidden when any path segment is dot-leading.
export function isHiddenRel(rel: string): boolean {
  return rel.startsWith(".") || rel.includes("/.");
}

// ONE collator for every comparison in this module.
//
// `rel.localeCompare(other, undefined, { sensitivity: "base" })` has to build
// an ICU collator from the options bag on every single call, and the
// comparator runs ~n·log n times: sorting a 200k-hit list (what a
// one-character query over an index-backed corpus produces) spent 3.8 of its
// 4.7 seconds inside localeCompare alone — the typing freeze. A hoisted
// collator is the SAME collation at ~2% of the cost.
const collator = new Intl.Collator(undefined, { sensitivity: "base" });
const byRel = collator.compare;

// Path depth, counted without allocating: `split("/")` in the comparator
// built two throwaway arrays per comparison. Computed once per hit instead
// (see SearchHit.depth), which is n rather than n·log n.
function depthOf(rel: string): number {
  let depth = 1;
  for (let i = 0; i < rel.length; i++) if (rel.charCodeAt(i) === 47) depth++;
  return depth;
}

// How much of the match landed on the entry's OWN name: 1 = the query is a
// substring of the name, 2 = the name matched only fuzzily, 3 = only ancestor
// directories matched. Derived from what the matcher already returned plus the
// lowercased name scoreEntries already slices for its exact/prefix bonuses —
// calling fuzzyMatch a second time (against the name alone) would double the
// cost of the hot path, and the 150k-hit budget in search.test.ts does not have
// room for that.
function nameTier(name: string, nameStart: number, q: string,
                  positions: number[]): 1 | 2 | 3 {
  if (name.includes(q)) return 1;
  // Every matched char sits before the name: the hit is entirely in ancestors.
  if (positions.length && positions[positions.length - 1] < nameStart) return 3;
  return 2;
}

export function rankCompare(a: SearchHit, b: SearchHit): number {
  if (b.longestRun !== a.longestRun) return b.longestRun - a.longestRun;
  // Above `score`, because scoring runs over the whole rel path and a matching
  // ancestor directory therefore donates its score to every descendant: query
  // "render" scored render/a/b/c/d/e/f/deep-thing.bin at 26 and myrender.ts at
  // 21, and `depth` below is only reachable on an exact score tie.
  //
  // BELOW `longestRun`, which is what already guarantees substring-over-fuzzy:
  // fuzzyMatch's substring branch sets longestRun = q.length (the maximum the
  // subsequence branch can never reach), so mycfgfile.txt beats c/f/g/notes.txt
  // before the tier is consulted. That invariant must survive any change here.
  if (a.tier !== b.tier) return a.tier - b.tier;
  if (b.score !== a.score) return b.score - a.score;
  if (a.depth !== b.depth) return a.depth - b.depth;
  return byRel(a.entry.rel, b.entry.rel);
}

// Score `entries[from..]` against the query (unsorted — callers sort with
// rankCompare after merging). `showHidden=false` skips dot-entries before
// scoring (see queryWantsHidden). The `from` offset is what makes streaming
// cheap: each flush scores only the entries that arrived since the last one.
//
// On top of the fuzzy score, the entry NAME (last path segment) gets intent
// bonuses: an exact name match outranks everything ("Downloads" must beat
// "DownloadStage", whose extra camel-hump bonus otherwise wins), and a name
// starting with the query beats an interior hit. Char-level heuristics can't
// express "this IS the thing you typed", so it's layered here, not in fuzzy.ts.
// `to` bounds one SLICE of a chunked scan (default: to the end). Scoring the
// whole corpus in one call blocks the main thread for as long as it takes;
// the caller walks it in slices and yields between them (listing/scan-job).
export function scoreEntries(
  query: string,
  entries: WalkEntry[],
  from: number,
  showHidden: boolean,
  to: number = entries.length,
): SearchHit[] {
  const q = query.toLowerCase();
  const hits: SearchHit[] = [];
  const end = Math.min(to, entries.length);
  for (let i = from; i < end; i++) {
    const entry = entries[i];
    if (!showHidden && isHiddenRel(entry.rel)) continue;
    const m = fuzzyMatch(query, entry.rel);
    if (!m) continue;
    let score = m.score;
    const nameStart = entry.rel.lastIndexOf("/") + 1;
    const name = entry.rel.slice(nameStart).toLowerCase();
    if (name === q) score += 100;
    else if (name.startsWith(q)) score += 25;
    hits.push({ entry, positions: m.positions, score, longestRun: m.longestRun,
                tier: nameTier(name, nameStart, q, m.positions),
                depth: depthOf(entry.rel) });
  }
  return hits;
}
