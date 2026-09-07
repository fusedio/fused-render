// A ranked answer from the server, as the rows the listing already renders.
//
// `/api/index/rank` returns the ORDER and the scoring fields, and deliberately
// not the match positions: the client re-runs `fuzzyMatch` over the ~200 rows
// it got back, so platform/lib/fuzzy.ts stays the single source of truth for
// what highlights, and the server stays free to carry positions internally
// (or not at all — `fused_render/index/query.py`'s `_rank_sql` scores in SQL
// and never materializes them) without that becoming a wire contract.
//
// The two rankers agree on substring hits (index-backed search is substring-
// only; tests/fixtures/rank-parity.json, restricted to substring rows, pins
// that in both languages) — but this file does not depend on it: a row the
// browser's matcher refuses loses its HIGHLIGHT and keeps its place. Dropping
// it instead would mean a file that exists, that the server ranked, and that
// the search cannot find. In practice this never fires for a ranked hit:
// `fuzzyMatch`'s own substring branch matches whenever `q` is literally a
// substring of `h.rel`, which is exactly what `/api/index/rank` already
// filtered on server-side.
import { fuzzyMatch } from "@platform/lib/fuzzy";
import type { IndexRankHit } from "@platform/lib/api";
import type { SearchHit } from "@apps/explorer/listing/types";

function tierOf(tier: number): 1 | 2 | 3 {
  // The wire is not the type system, and everything downstream orders on this.
  return (tier <= 1 ? 1 : tier >= 3 ? 3 : 2) as 1 | 2 | 3;
}

/** The server's ranked hits as `SearchHit`s, in the order it returned them. */
export function hitsFromRank(hits: IndexRankHit[], q: string): SearchHit[] {
  if (!q) return [];
  return hits.map((h) => ({
    entry: { rel: h.rel, is_dir: h.is_dir, size: h.size, mtime: h.mtime },
    positions: fuzzyMatch(q, h.rel)?.positions ?? [],
    score: h.score,
    longestRun: h.longest_run,
    tier: tierOf(h.tier),
    depth: h.depth,
  }));
}
