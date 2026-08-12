// Stale-while-revalidate for the in-folder search's CORPUS (the rows to rank),
// as distinct from lib/search-hold which does it for the ranked RESULTS.
//
// The two are not interchangeable, and that is the whole reason this exists.
// search-hold is query-tagged on purpose — held rows are only ever rendered
// under the query they were computed for — so it deliberately gives nothing
// back when the query changes. A keystroke during a corpus refetch is exactly
// that case: the fetch effect publishes an empty `streaming` state before the
// request resolves, so the new query ranked against an empty array and the
// result list blanked. Holding the previous generation's ENTRIES instead lets
// the new query rank against a one-generation-stale corpus and paint
// immediately, which is a strictly better answer than none.
//
// What keeps that honest: the hold is only ever consulted while the current
// generation's walk is UNSETTLED, it is reported back as `stale: true` (the
// caller dims the rows and keeps the spinner up — Listing's `listing-stale`),
// and it is dropped the instant the fresh corpus has anything to say. A
// SETTLED walk always wins, including a settled EMPTY one: "the file was just
// deleted" is a real answer and must not be papered over by yesterday's rows.
import type { WalkEntry } from "@platform/lib/api";
import type { WalkState } from "@apps/explorer/listing/types";

export interface HeldCorpus {
  entries: WalkEntry[];
  /** The corpus identity this array carries — see WalkState.key. */
  key: string;
}

export interface ScannableCorpus {
  /** The rows to rank, or null when there is nothing to rank at all. */
  entries: WalkEntry[] | null;
  /** WalkState.key of `entries`; "" when there are none. */
  key: string;
  /** These rows are a generation behind. The caller must SAY so. */
  stale: boolean;
}

const NOTHING: ScannableCorpus = { entries: null, key: "", stale: false };

/**
 * What to retain as the fallback corpus after this render.
 *
 * Any walk with rows in it — settled or still streaming — replaces the hold;
 * a partial corpus is still a better stand-in than an empty one. Anything else
 * keeps whatever was already held, identity-stable so a re-render of the same
 * corpus does not churn the callers keyed on it.
 */
export function nextHeldCorpus(walk: WalkState, held: HeldCorpus | null): HeldCorpus | null {
  if ((walk.status === "ok" || walk.status === "streaming") && walk.entries.length > 0) {
    return held !== null && held.entries === walk.entries ? held : { entries: walk.entries, key: walk.key };
  }
  return held;
}

/**
 * The corpus to rank right now, and whether it is a generation behind.
 *
 * An errored walk yields nothing deliberately: the error is what the user is
 * shown, and standing rows in front of it would hide a failure they can retry.
 */
export function scannableCorpus(
  searching: boolean,
  walk: WalkState,
  held: HeldCorpus | null,
): ScannableCorpus {
  if (!searching) return NOTHING;
  // A settled walk is the answer, even when it is empty.
  if (walk.status === "ok") return { entries: walk.entries, key: walk.key, stale: false };
  if (walk.status === "streaming" && walk.entries.length > 0)
    return { entries: walk.entries, key: walk.key, stale: false };
  if ((walk.status === "idle" || walk.status === "streaming") && held !== null)
    return { entries: held.entries, key: held.key, stale: true };
  return NOTHING;
}
