// The zero-match glob-broadening offer's reserved row-path.
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
