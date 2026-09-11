// A ranked answer from the server, as the rows the listing already renders.
//
// `/api/index/rank` returns the ORDER, not scoring fields: `_rank_sql` ranks
// server-side and nothing downstream re-sorts a server-answered row, so this
// file hands back hits "in the order it returned them", full stop. Match
// positions are a separate story: the client re-runs `substringMatch`
// (platform/lib/fuzzy.ts) over the ~200 rows it got back — not the looser
// `fuzzyMatch`, because every row here already passed `/api/index/rank`'s own
// substring filter, so this is the exact test that guarantee is stated in
// terms of, not merely a weaker test that happens to agree with it.
//
// The two rankers agree on substring hits (index-backed search is substring-
// only; tests/fixtures/rank-parity.json, restricted to substring rows, pins
// that in both languages) — but this file does not depend on it: a row the
// browser's matcher refuses loses its HIGHLIGHT and keeps its place. Dropping
// it instead would mean a file that exists, that the server ranked, and that
// the search cannot find. In practice this rarely fires for a ranked hit: the
// one known gap is a folding disagreement between DuckDB's `lower()` and
// JS's, at the same kind of Unicode edge case `_rank_sql` (query.py) already
// has to special-case server-side (D712).
//
// A glob-mode query is a third case, and it is not a disagreement to
// reconcile: a glob hit is not promised to be a substring of the query text
// in the first place (`*.csv` matching `report.csv` has no literal `"*.csv"`
// anywhere in the path), so `substringMatch` is never even run over one —
// `hitsFromRank`'s `mode` parameter skips straight to the unhighlighted,
// row-kept outcome instead of asking a test that was never going to apply.
import { substringMatch } from "@platform/lib/fuzzy";
import type { IndexRankHit } from "@platform/lib/api";
import type { SearchHit } from "@apps/explorer/listing/types";

/** The server's ranked hits as `SearchHit`s, in the order it returned them.
 *
 * `mode` is which matcher the server actually ran (`IndexRankResult.mode`),
 * and it decides how a hit's highlight is recomputed: a substring-mode hit
 * still gets `substringMatch` re-run over it (see module comment for why),
 * but a glob-mode hit is never re-tested that way. A glob match like
 * `*.csv` against `report.csv` has no literal substring relationship to the
 * query text at all, so running `substringMatch` over it would be testing
 * something the match was never promised to satisfy — it renders unhighlighted
 * instead, same as any other hit the browser's matcher does not confirm, but
 * without ever risking a false "no highlight, real hit" verdict standing in
 * for a mislabeled coincidental one. Defaults to `"substring"`. */
export function hitsFromRank(
  hits: IndexRankHit[],
  q: string,
  mode: "substring" | "glob" = "substring",
): SearchHit[] {
  if (!q) return [];
  // A query that escapes the box root (query-base.ts's `escapesBase`) carries
  // a base prefix `resolve_query` (fused_render/index/query.py) has already
  // consumed into `res.base` — `h.rel` is relative to THAT, not to the query
  // as typed, so the consumed prefix can never appear as a literal substring
  // of `rel`. The segment after the query's last "/" is what the server's own
  // walk actually matched against, so it is retried against `rel` whenever
  // the full query refuses — for a query with no "/" this is the same string,
  // so the fallback is a no-op there rather than a second, different test.
  const leaf = q.slice(q.lastIndexOf("/") + 1);
  return hits.map((h) => {
    let positions: number[] = [];
    if (mode === "substring") {
      positions =
        substringMatch(q, h.rel)?.positions ??
        (leaf !== q ? substringMatch(leaf, h.rel)?.positions : undefined) ??
        [];
    }
    return { entry: { rel: h.rel, is_dir: h.is_dir, size: h.size, mtime: h.mtime }, positions };
  });
}
