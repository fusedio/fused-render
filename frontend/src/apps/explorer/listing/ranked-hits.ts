// A ranked answer from the server, as the rows the listing already renders.
//
// `/api/index/rank` returns the ORDER, not the scoring fields: `score`/
// `tier`/`depth`/`longest_run` drove `_rank_sql`'s ORDER BY server-side, but
// nothing downstream re-sorts a server-answered row — this file hands back
// hits "in the order it returned them", full stop — so the server stops at
// computing them and the wire (`IndexRankHit`) never carried them. `SearchHit`
// still declares those fields (the live-walk path's `rankCompare` genuinely
// needs them), so this function fills them with placeholders rather than
// dropping them from the shared type; nothing reads a placeholder because
// nothing re-sorts this path. Match positions are the same story: the client
// re-runs `substringMatch` (platform/lib/fuzzy.ts) over the ~200 rows it got
// back — not the looser `fuzzyMatch`, because every row here already passed
// `/api/index/rank`'s own substring filter, so this is the exact test that
// guarantee is stated in terms of, not merely a weaker test that happens to
// agree with it.
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
 * `score`/`longestRun`/`tier`/`depth` are placeholders — not on the wire, and
 * not read on this path (see module comment).
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
 * for a mislabeled coincidental one. Defaults to `"substring"` so the walk
 * path (which has no glob mode) is unaffected. */
export function hitsFromRank(
  hits: IndexRankHit[],
  q: string,
  mode: "substring" | "glob" = "substring",
): SearchHit[] {
  if (!q) return [];
  return hits.map((h) => ({
    entry: { rel: h.rel, is_dir: h.is_dir, size: h.size, mtime: h.mtime },
    positions: mode === "substring" ? (substringMatch(q, h.rel)?.positions ?? []) : [],
    score: 0,
    longestRun: q.length,
    tier: 1,
    depth: 0,
  }));
}
