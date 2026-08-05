// Fuzzy scoring and ranking for the in-folder search (over the streamed walk).
import type { WalkEntry } from "@platform/lib/api";
import { fuzzyMatch } from "@platform/lib/fuzzy";
import type { SearchHit, SortKey, SortOrder } from "@apps/explorer/listing/types";

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

export function rankCompare(a: SearchHit, b: SearchHit): number {
  if (b.longestRun !== a.longestRun) return b.longestRun - a.longestRun;
  if (b.score !== a.score) return b.score - a.score;
  const ad = a.entry.rel.split("/").length;
  const bd = b.entry.rel.split("/").length;
  if (ad !== bd) return ad - bd;
  return a.entry.rel.localeCompare(b.entry.rel, undefined, { sensitivity: "base" });
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
export function scoreEntries(
  query: string,
  entries: WalkEntry[],
  from: number,
  showHidden: boolean,
): SearchHit[] {
  const q = query.toLowerCase();
  const hits: SearchHit[] = [];
  for (let i = from; i < entries.length; i++) {
    const entry = entries[i];
    if (!showHidden && isHiddenRel(entry.rel)) continue;
    const m = fuzzyMatch(query, entry.rel);
    if (!m) continue;
    let score = m.score;
    const name = entry.rel.slice(entry.rel.lastIndexOf("/") + 1).toLowerCase();
    if (name === q) score += 100;
    else if (name.startsWith(q)) score += 25;
    hits.push({ entry, positions: m.positions, score, longestRun: m.longestRun });
  }
  return hits;
}

// Column-sort a ranked result set (the search headers' asc/desc modes; null
// searchSort — relevance — leaves the rankCompare order untouched upstream).
export function sortHits(hits: SearchHit[], sort: SortKey, order: SortOrder): SearchHit[] {
  const flip = order === "desc" ? -1 : 1;
  const byName = (a: SearchHit, b: SearchHit) =>
    a.entry.rel.localeCompare(b.entry.rel, undefined, { sensitivity: "base" });
  return [...hits].sort((a, b) => {
    let cmp: number;
    if (sort === "size") cmp = (a.entry.size ?? -1) - (b.entry.size ?? -1);
    else if (sort === "mtime") cmp = (a.entry.mtime ?? 0) - (b.entry.mtime ?? 0);
    else cmp = byName(a, b);
    if (cmp === 0) cmp = byName(a, b);
    return cmp * flip;
  });
}
