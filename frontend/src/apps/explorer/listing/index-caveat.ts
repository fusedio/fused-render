// The scan caveat shown in the search box's status chip while the file index
// is being (re)built.
//
// Pure and separate from Listing.tsx because it is the one piece of that
// string a test can meaningfully pin: which of the two messages appears is a
// claim about how much the results can be trusted, and getting it backwards
// would tell the user their answers are stale when they are live, or the
// reverse.
import type { IndexStatus } from "@platform/lib/api";

export interface IndexCaveat {
  note: string;
  title: string;
}

// `has_index` splits the two scanning cases. An index that already exists keeps
// answering while a rescan runs (the last completed generation), so the user
// is told the results may lag. With no index yet the live walk is answering,
// so the same spinner means progress, not staleness.
//
// `behind` is the third message and the quiet one: no scan is running, but
// the file index itself moved since these results were computed (a completed
// scan bumping `useListingSearch.ts`'s lifecycle count — not a bare dir-watch
// event, which is background churn under the tree that says nothing about
// whether the index changed, and does not set this) and the search is
// deliberately not refetching (listing/revalidate — swapping the rows out
// from under someone reading them is worse than being a little behind). That
// trade is only defensible if it is stated, which is what this says. A
// running scan outranks it: "indexing…" already implies the same caveat and
// names the reason.
// `rescanPending` is the third input and the one with no poll behind it: this
// app just changed a file, the server has been told to rescan that folder
// (server/index_touch.py), and until a status poll catches the run, `scanning`
// is still false while the rows on screen are the ones that are wrong. It
// reads as the same "indexing…" because it is the same claim — results come
// from an index that is being put right — and it outranks `behind`, which says
// nothing is coming.
//
// `failed` is the fourth input: the request FOR THE QUERY NOW IN THE BOX
// errored, so the rows on screen answer whatever the last request that
// succeeded was asked. It outranks `behind`'s generic "not refreshed" —
// that phrasing promises a plain re-run will catch up, which is not what
// just happened here — but a running scan is still the more urgent claim
// when both are true, so it is checked first.
export function indexCaveat(
  status: IndexStatus | null | undefined,
  behind = false,
  rescanPending = false,
  failed = false,
): IndexCaveat | null {
  if (rescanPending && !(status && status.scanning)) {
    return {
      note: "indexing…",
      title:
        "This folder was just changed here, so it is being re-indexed. Results may still show it as it was a moment ago.",
    };
  }
  if (status && status.scanning) {
    if (status.has_index) {
      return {
        note: "indexing…",
        title:
          "A scan is running. Results come from the last completed index, so a very recent change may be missing.",
      };
    }
    return {
      note: `building index… ${(status.files || 0).toLocaleString()} files`,
      title:
        "Building the file index for the first time. This folder is being searched live meanwhile.",
    };
  }
  if (failed) {
    return {
      note: "search failed",
      title:
        "The last search request failed, so these results still answer an earlier query. Edit the search or press Enter again to retry.",
    };
  }
  if (behind) {
    return {
      note: "not refreshed",
      title:
        "The file index changed since these results were computed. They are kept as they are rather than swapped out while you read them — clear the search and run it again for the newest.",
    };
  }
  return null;
}

// The chip carries both facts when both exist — one element, because the chip
// is absolutely pinned inside the input and a second one would compete with it
// for the same pixels on a narrow pane.
export function withCaveat(count: string | null, caveat: IndexCaveat | null): string | null {
  if (!caveat) return count;
  return count ? `${count} · ${caveat.note}` : caveat.note;
}

/**
 * The caveat for a box that asks the server per query.
 *
 * Both search boxes assemble the same three inputs, and the assembly is where
 * they got it wrong rather than in `indexCaveat` itself — which is why it is a
 * function now instead of an expression repeated at two call sites with a
 * test grepping for the word `pending`.
 *
 * `behind` — "these rows answer a different query, or an older generation of
 * the tree" — is two situations wearing one name. While a request is in
 * flight, or is merely scheduled and still sitting out the debounce before it
 * goes out, the next answer is at most a couple hundred ms away, and
 * captioning that "not refreshed… clear the search and run it again" is
 * corpus-staleness language for a round trip that has not even started yet.
 * `pending` covers both — armed the moment a request is scheduled, not only
 * once it is in flight — so this guard's window matches the whole wait, not
 * just its back half. Only rows that are STUCK are stale.
 *
 * `failed` — the request for the query now in the box errored, and the rows
 * on screen answer whatever the last request that succeeded was asked —
 * folds into the same `behind` question (`useListingSearch.ts`'s `behind`
 * is already true whenever `failed` is) but carries its own caption rather
 * than `indexCaveat`'s generic "not refreshed" one, which promises a plain
 * re-run will catch up. Optional: `FilesHome.tsx`'s own search can fail the
 * same way, but it already says so through its own `ErrorBanner` row rather
 * than this chip, so it never passes `failed` here.
 */
export function searchCaveat(
  status: IndexStatus | null | undefined,
  state: { behind: boolean; pending: boolean; rescanPending: boolean; failed?: boolean },
): IndexCaveat | null {
  return indexCaveat(status, state.behind && !state.pending, state.rescanPending, state.failed);
}
