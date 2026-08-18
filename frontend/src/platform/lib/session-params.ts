// WHICH URL PARAMS NOTHING MAY PERSIST — the params that describe the CHROME
// around a file rather than the file, and so must never outlive the page session
// (LSN-12, D326). One name so far: `_side`.
//
// WHY `_side` IS ON THE LIST: the companion sidebar's open/closed state and
// chosen mode (apps/explorer/lib/preview-side.ts, whose header has the
// argument) is session-only BY POLICY — the sidebar opens at its default on
// every page load, and a refresh is the way back from any change to it. Anything
// that writes `_side` down and replays it later breaks exactly that: while the
// per-file session sidecar recorded it, opening the sidebar on one file made
// that file come up with a sidebar for good while its neighbour never did, and
// no refresh could undo it, because a refresh is precisely when a sidecar is
// replayed. The owner's report was that plainly — "we don't want any persisted
// preference".
//
// THE SIDECAR IS NO LONGER A CONSUMER (D329): the per-file session restore that
// this rule was written for is gone, and with it the SERVER half of the policy
// (`server/session.py`'s `_strip_side`). What remains is the RECENTS store —
// `apps/explorer/lib/recents.ts`, whose rows must hold what the file was, not
// what the chrome around it was doing — so this is now a single-implementation
// rule with a single caller, not two halves that had to agree.
//
// Kept DOM-free and separate from its caller for the reason the other decision
// modules are (apps/explorer/lib/side-width.ts states it): the rule is exactly
// the part a bun test should pin, without importing the hooks and network around
// it.
const SESSION_OMIT = new Set(["_side"]);

// TEXTUAL, not URLSearchParams: a stored/recorded query is the shell's query
// string VERBATIM, and round-tripping it would quietly rewrite a template's own
// params (`q=a+b%2Cc`, `stretch=2,1471`) on every record.
export function stripSessionParams(search: string): string {
  if (search === "") return "";
  return search
    .split("&")
    .filter((p) => p !== "" && !SESSION_OMIT.has(p.split("=", 1)[0]))
    .join("&");
}
