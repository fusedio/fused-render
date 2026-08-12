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
// these results were computed from an older generation of the tree and the
// search is deliberately not refetching (listing/revalidate — swapping the
// rows out from under someone reading them is worse than being a little
// behind). That trade is only defensible if it is stated, which is what this
// says. A running scan outranks it: "indexing…" already implies the same
// caveat and names the reason.
export function indexCaveat(
  status: IndexStatus | null | undefined,
  behind = false,
): IndexCaveat | null {
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
  if (behind) {
    return {
      note: "not refreshed",
      title:
        "This folder or the file index changed since these results were computed. They are kept as they are rather than swapped out while you read them — clear the search and run it again for the newest.",
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
