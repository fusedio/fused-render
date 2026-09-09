// The search results display cap, and the counter text that owns up to it.
//
// Scoring and ranking still run over the ENTIRE corpus — this caps the list,
// not the search. Past the first hundred a fuzzy rank has stopped saying
// anything the user can act on, and the useful move is a better query rather
// than more scrolling, so the tail is not rendered and the counter says how
// much it is not showing. That is also why there is no "load more": offering
// one would answer the wrong question. The counter states the fact and stops
// there — telling the user to "refine your search" is instruction, not
// information, and they can see the number for themselves.
//
// The count stays TRUE. Reporting the capped number would be a lie about the
// folder, and the whole point of the message is to tell the user how much they
// are not seeing.
import { SEARCH_RESULT_CAP, type SearchHit } from "@apps/explorer/listing/types";

/**
 * The rows to render.
 *
 * A ranked (substring) answer keeps the top-N display cap above — past the
 * first hundred a fuzzy rank has stopped saying anything actionable. A glob
 * answer has no such tail: every hit is an equal match, so every FETCHED hit
 * renders (decision 9 — on-screen equals fetched equals selectable), and the
 * only ceiling left is the server's own fetch limit (SEARCH_GLOB_RANK_LIMIT).
 */
export function capHits(hits: SearchHit[], mode: "substring" | "glob" = "substring"): SearchHit[] {
  if (mode === "glob") return hits;
  return hits.length <= SEARCH_RESULT_CAP ? hits : hits.slice(0, SEARCH_RESULT_CAP);
}

/**
 * The match-count chip's text.
 *
 * `truncated` is the server's own rank-limit cap (SEARCH_RANK_LIMIT /
 * SEARCH_GLOB_RANK_LIMIT) — a separate, pre-existing "there was more than
 * this" that the number carries as a `+`. It has to survive the display cap:
 * the two truncations are independent and both are true at once on a large
 * tree. A glob answer has no display cap of its own (capHits above), so its
 * label never says "Showing top N of" — the count already IS what rendered.
 */
export function resultCountLabel(
  total: number,
  truncated: boolean,
  mode: "substring" | "glob" = "substring",
): string {
  const suffix = truncated ? "+" : "";
  const n = total.toLocaleString();
  if (mode === "glob" || total <= SEARCH_RESULT_CAP) {
    return `${n}${suffix} match${total === 1 ? "" : "es"}`;
  }
  return `Showing top ${SEARCH_RESULT_CAP} of ${n}${suffix}`;
}
