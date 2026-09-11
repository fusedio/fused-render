// Whether the listing body shows committed SEARCH HITS (one Path column) or
// the FOLDER'S OWN rows (Name/Size/Modified) — the one place this choice is
// made. Listing.tsx reads it for the column count, the <thead>, and the body
// branch together, so the three can never disagree.
//
// `searchState.status === "idle"` covers not searching at all AND a typed-
// but-uncommitted query with no answer ever fetched (decision 4's Enter
// gate). `awaitingCommit` covers a typed-but-uncommitted query sitting over a
// PREVIOUS committed answer's rows (the `behind`/`awaitingCommit` split,
// DECISIONS-one-field-search.md). Neither is a set of search hits to show:
// both fall back to the folder's own rows, with the Enter prompt rendered as
// a banner row above them rather than replacing the body.
import type { SearchState } from "@apps/explorer/listing/types";

export function showingSearchHits(searchState: SearchState, awaitingCommit: boolean): boolean {
  return searchState.status !== "idle" && !awaitingCommit;
}
