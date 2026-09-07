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
// re-runs `fuzzyMatch` over the ~200 rows it got back, so platform/lib/
// fuzzy.ts stays the single source of truth for what highlights.
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

/** The server's ranked hits as `SearchHit`s, in the order it returned them.
 * `score`/`longestRun`/`tier`/`depth` are placeholders — not on the wire, and
 * not read on this path (see module comment). */
export function hitsFromRank(hits: IndexRankHit[], q: string): SearchHit[] {
  if (!q) return [];
  return hits.map((h) => ({
    entry: { rel: h.rel, is_dir: h.is_dir, size: h.size, mtime: h.mtime },
    positions: fuzzyMatch(q, h.rel)?.positions ?? [],
    score: 0,
    longestRun: q.length,
    tier: 1,
    depth: 0,
  }));
}
