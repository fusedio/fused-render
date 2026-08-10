// The search results display cap, and the counter text that owns up to it.
//
// Scoring and ranking still run over the ENTIRE corpus — this caps the list,
// not the search. Past the first hundred a fuzzy rank has stopped saying
// anything the user can act on, and the useful move is a better query rather
// than more scrolling, so the tail is not rendered and the counter says why.
// That is also why there is no "load more": offering one would answer the
// wrong question.
//
// The count stays TRUE. Reporting the capped number would be a lie about the
// folder, and the whole point of the message is to tell the user how much they
// are not seeing.
import { SEARCH_RESULT_CAP, type SearchHit } from "@apps/explorer/listing/types";

/** The rows to render: the top of the ranking, in rank order. */
export function capHits(hits: SearchHit[]): SearchHit[] {
  return hits.length <= SEARCH_RESULT_CAP ? hits : hits.slice(0, SEARCH_RESULT_CAP);
}

/**
 * The match-count chip's text.
 *
 * `walkTruncated` is the server's own entry cap on the walk — a separate,
 * pre-existing "there was more than this" that the number carries as a `+`.
 * It has to survive the display cap: the two truncations are independent and
 * both are true at once on a big tree.
 */
export function resultCountLabel(total: number, walkTruncated: boolean): string {
  const suffix = walkTruncated ? "+" : "";
  const n = total.toLocaleString();
  if (total <= SEARCH_RESULT_CAP) {
    return `${n}${suffix} match${total === 1 ? "" : "es"}`;
  }
  return `Showing top ${SEARCH_RESULT_CAP} of ${n}${suffix} — refine your search`;
}
