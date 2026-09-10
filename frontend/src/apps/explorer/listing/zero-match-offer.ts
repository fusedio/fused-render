// The zero-match glob-broadening offer: its reserved row-path, and the
// derivation of whether the offer should exist at all.
//
// A committed PATTERN (glob) search that settles on zero hits renders one
// offer row — "no matches for the pattern typed, here is the recursively
// broadened one, press Enter or click to rerun it" (Listing.tsx, the
// `broadenedPattern` branch of the settled-empty body). That row is wired
// through the exact same two places every real row is activated from —
// Listing.tsx's `onRowPointerUp` and useListingSelection.ts's Enter case —
// rather than a third, parallel activation path. Both call sites need to
// agree on ONE thing without string-comparing a literal in two places: is
// the "row" in front of them this offer, or a real filesystem entry that
// should navigate?
//
// A NUL byte answers that unambiguously: it is illegal inside a POSIX path
// (the kernel rejects it outright) and inside a Windows path (disallowed by
// every Win32 file API), so no real row's path can ever equal this string,
// typed or synced from any filesystem this app can list.
export const ZERO_MATCH_OFFER_PATH = "\0zero-match-broaden-offer";

export function isZeroMatchOfferPath(path: string): boolean {
  return path === ZERO_MATCH_OFFER_PATH;
}

// --- whether the offer should exist at all -----------------------------------
//
// This is deliberately its own small function, not five booleans read
// inline at the call site: `mode`, `reason` and the rendered hits are all
// read off useListingSearch's `answer` object, which only changes when a
// new answer actually lands — so those three move in lockstep WITH EACH
// OTHER. They do not move in lockstep with the query in the box. `q`
// (`useDeferredValue`) catches up to a keystroke well before the fetch
// effect's own trailing debounce fires the request that would produce a new
// answer for it, and `scanPending` (`pending || polling`) stays false for
// that whole wait — `pending` is not set until the debounce elapses and the
// request actually goes out. A "settled" check built only from
// `!scanPending` and the `searchState` status therefore reads that debounce
// window as settled: the box already shows the next query's text,
// `displayHits`/`mode`/`reason` still describe the PREVIOUS one, and nothing
// has checked the new text for zero hits at all — the offer would rerun a
// query that was never actually searched for.
//
// `rowsAnswerQuery` (useListingSearch.ts) is required here for exactly that
// reason: it compares the answer on screen against `q` directly, so it goes
// false the instant the box moves past what the on-screen answer is for —
// no request needs to have gone out yet, let alone landed. Requiring it
// turns "five flags that have to be trusted to move together" into "the one
// flag that already says which query the rows on screen answer."
import type { RankReason } from "@platform/lib/api";
import { broadenGlobOffer, type BroadenOffer } from "@apps/explorer/listing/glob-broaden";
import type { SearchState } from "@apps/explorer/listing/types";

export interface ZeroMatchOfferInput {
  /** The search body is on screen at all (search-body-mode.ts). */
  showsSearchHits: boolean;
  /**
   * The rows/mode/reason on screen actually answer `q` — see the module
   * header for why this, and not `scanPending`/`searchState` alone, is what
   * closes the debounce-window gap.
   */
  rowsAnswerQuery: boolean;
  searchState: SearchState;
  /** A request is genuinely in flight or a scan is being polled. */
  scanPending: boolean;
  displayHitsLength: number;
  mode: "substring" | "glob";
  reason: RankReason;
  /** The deferred, already-settled query text the offer would widen. */
  q: string;
}

export function settledZeroMatchOffer(input: ZeroMatchOfferInput): BroadenOffer | null {
  const {
    showsSearchHits,
    rowsAnswerQuery,
    searchState,
    scanPending,
    displayHitsLength,
    mode,
    reason,
    q,
  } = input;
  const settledEmpty =
    showsSearchHits &&
    rowsAnswerQuery &&
    searchState.status !== "error" &&
    searchState.status !== "pending" &&
    !scanPending &&
    displayHitsLength === 0;
  if (!settledEmpty || mode !== "glob" || reason !== "") return null;
  return broadenGlobOffer(q);
}
