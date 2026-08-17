// WHICH URL PARAMS A SESSION SIDECAR MAY NOT CARRY (LSN-12, D323). One name so
// far: `_side`.
//
// Split out of session.ts and DOM-free for the reason the other decision modules
// are (apps/explorer/lib/side-width.ts states it): session.ts is hooks and network
// and reads `location` at module scope, so a bun test cannot import it, and this
// rule is exactly the part a test should pin. It is also the frontend half of a
// rule the SERVER states too (`fused_render/server/session.py`'s `_strip_side`),
// which is the other reason it wants a name of its own — two implementations of one
// policy should at least both be pointing at something.
//
// WHY `_side` IS ON THE LIST: the sidecar exists to replay the query you last had
// on a file, which is exactly the wrong treatment for a param whose whole policy is
// "session only, and a refresh is the way back" — the companion sidebar's
// open/closed state and chosen mode (apps/explorer/lib/preview-side.ts, whose
// header has the argument). Left in, opening the sidebar on one file recorded it
// there for good: that file came up with a sidebar months later while its
// neighbour never did, and no refresh could undo it, because a refresh is precisely
// when the sidecar is replayed. The owner's report was that plainly — "we don't
// want any persisted preference".
//
// STRIPPED AT BOTH ENDS, and each end is load-bearing for its own reason: on the
// way OUT so nothing new is written, and on the way IN so the sidecars already on
// disk stop replaying what they recorded before this rule existed.
const SESSION_OMIT = new Set(["_side"]);

// TEXTUAL, not URLSearchParams: LSN-2 says the stored query is the shell's query
// string VERBATIM, and round-tripping it would quietly rewrite a template's own
// params (`q=a+b%2Cc`, `stretch=2,1471`) on every open of every file.
export function stripSessionParams(search: string): string {
  if (search === "") return "";
  return search
    .split("&")
    .filter((p) => p !== "" && !SESSION_OMIT.has(p.split("=", 1)[0]))
    .join("&");
}
