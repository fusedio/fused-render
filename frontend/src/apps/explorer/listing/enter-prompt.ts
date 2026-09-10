// Decision 9: the "Press Enter to search" placeholder shown while a
// path/pattern query sits uncommitted (decision 4's gate). Driven off
// decision 5's `typedAddress` — the same `TypedAddress` the Enter handler
// branches on, and checks FIRST — not off the gate alone: a resolved, real
// folder in the field means Enter navigates, not searches, and the prompt
// has to say so rather than promising a search next to a dropdown naming
// the exact folder Enter would open. Being a pure projection of
// `typedAddress` is what keeps the prompt and the handler from disagreeing.
import type { TypedAddress } from "@apps/explorer/listing/useTypedPathAddress";

// Decision 9 revisited: name the folder Enter is about to open, not just say
// "outside this folder" — pressing Enter here does two things (move the
// search base to the folder the query names, then search there), and the
// vague wording hid the first half. Splits on "/" and cuts at the first
// glob-bearing segment (`*` or `?`), the same segment shape `query-base.ts`
// and `completion-target.ts` already reason about, so `~/Work/*/*.json`
// yields `~/Work`. A query with no glob segment at all still has its last
// segment dropped — that segment is a name pattern to filter by, not part of
// the folder (`~/Work/notes` -> `~/Work`), matching how a plain filter word
// is already excluded from `completion-target.ts`'s dropdown.
//
// This does NOT walk the filesystem, so the named folder may not exist —
// `resolve_query` (fused_render/index/query.py's `_walk_from`) widens the
// search on a missing folder instead of failing, which this prompt cannot
// know about synchronously. Recorded as a known limitation in
// DECISIONS-one-field-search.md rather than solved here.
function folderToOpen(query: string): string | null {
  const segments = query.split("/");
  const globIdx = segments.findIndex((s) => /[*?]/.test(s));
  const folderSegments = globIdx === -1 ? segments.slice(0, -1) : segments.slice(0, globIdx);
  const folder = folderSegments.join("/");
  return folder || null;
}

// Finding 3 (code review): a COMMITTED path-shaped query (Enter already
// pressed — `useListingSearch.ts`'s `gateOpen`) that resolves to no real
// filesystem entry (`typedAddress.status === "missing"`) was a silent dead
// end — `enterPrompt` above is never called for it at all (Listing.tsx's
// banner excludes every `isPathQuery` unconditionally), no rank request is
// coming (`isPathQuery` suppresses it by design), and the footer shows the
// folder's own item count, so nothing on screen says the query was refused.
//
// This is a REPORT, not an instruction — the user already pressed Enter and
// got their answer — so it does not reuse `enterPrompt`'s "Press Enter to
// …" phrasing (that would promise a second Enter will do something, and
// nothing will). Named the same way the "exists" branch above names a
// resolved address (its trailing-slash-stripped last segment), since the
// two are symmetric: one says what Enter opened, this says what it could
// not find.
export function pathNotFoundMessage(query: string): string {
  const trimmed = query.trim().replace(/\/+$/, "");
  const name = trimmed.split("/").pop() || trimmed;
  return `No such file or folder: ${name}`;
}

export function enterPrompt(typedAddress: TypedAddress, query: string): string {
  // Resolved to a real path: name it. Whether it's a file or a folder, Enter
  // opens it, not a search.
  if (typedAddress.status === "exists") {
    const trimmed = typedAddress.path.replace(/\/+$/, "");
    const name = trimmed.split("/").pop() || trimmed;
    return `Press Enter to open ${name}`;
  }
  // "checking" is still resolving — do not flip the wording into a third
  // state that appears and vanishes mid-keystroke. Enter falls through to
  // the search commit while unresolved, so the search wording is what is
  // actually true right now, same as "idle" and "missing".
  //
  // This function's one call site (Listing.tsx) only reaches here for a
  // query whose base escapes the folder being searched
  // (listing/query-base.ts's `escapesBase`) — a same-base query never gates
  // and never shows this. `folderToOpen` names that base from the query
  // text itself (no server round trip); when the query has nothing left to
  // name (bare `~`, bare `/`, or a `..` with nothing after it once
  // `typedAddress` has not already resolved it above), the generic phrasing
  // below stands in rather than printing an empty name.
  const folder = folderToOpen(query);
  return folder ? `Press Enter to open ${folder} and search` : "Press Enter to open that folder and search";
}
