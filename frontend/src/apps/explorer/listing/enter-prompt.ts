// FINDING 6 (code review, 2026-09-10): the "Press Enter to search" banner
// this file originally existed for (`enterPrompt`, decisions 9/9-revisited)
// was removed from Listing.tsx by SPEC-omnibox-search-affordance.md scope
// item 5 — its coverage moved into the dropdown's own action row
// (search-action-rows.ts's `searchAffordance`) instead. `enterPrompt` itself
// went unreferenced outside its own test the moment that landed; deleted
// here along with that test, per the spec's own instruction to check
// `tests/` for the symbol first (nothing there references it — confirmed by
// grep — `test_github_login.py`'s "Press Enter to open github.com..." is an
// unrelated CLI prompt). `folderToOpen` below survives: `searchAffordance`
// reuses it directly (finding 2) to name the folder a gated, escaping query
// would actually search, rather than inventing a second way to compute it.
// `pathNotFoundMessage` survives too — search-action-rows.ts's own
// not-found row reuses it verbatim.

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
export function folderToOpen(query: string): string | null {
  const segments = query.split("/");
  const globIdx = segments.findIndex((s) => /[*?]/.test(s));
  const folderSegments = globIdx === -1 ? segments.slice(0, -1) : segments.slice(0, globIdx);
  const folder = folderSegments.join("/");
  return folder || null;
}

// ITEM 10 (running-screen review, 2026-09-10): `folderToOpen` above returns
// DISPLAY text ("~/Work") — exactly what the offer row's own label should
// say, but not itself an fsPath `navigate()` can use: a leading "~" needs
// `home` to become an absolute path. Before this, the offer row's own
// label promised "Press Enter to open ~ and search" while `commitInPlace`
// only ever called `commitSearch()` — running the search but never
// actually opening the named folder, so the breadcrumb, the URL and the
// search-hit rows' own relative paths all disagreed about where the user
// was. This resolves the SAME text `folderToOpen` already names to the
// real fsPath `navigate()` needs, with the same `~`-expansion
// `escapesFsPath` (query-base.ts) already does for its own, narrower
// yes/no question — `home === undefined` returns null (nothing resolvable
// yet) the same way that predicate stays on the safe side when home has
// not loaded. An already-absolute or drive-letter folder (no leading "~")
// passes through unchanged, same as `folderToOpen` leaves it.
//
// FINDINGS 3 & 4 (code review, 2026-09-10) are the same root cause: this
// function does not touch the filesystem (that's the whole point of it —
// see above), so it cannot answer a question that requires walking real
// directories, and two shapes genuinely require that walk:
//
// - A `..` segment (finding 3): `folderToOpen("../*.md")` returns the bare
//   string `".."`, which is not an fsPath at all — `navigate` has no
//   current directory to climb it FROM (this function is never even given
//   one; only `query` and `home`). The server's own base resolution
//   (`resolve_query`, fused_render/index/query.py) walks a `..` segment
//   with `_walk_from`, which climbs however many real directories it
//   actually takes, exactly the computation this function would have to
//   duplicate — badly, and without seeing the disk — to get right.
// - A bare, non-`~`, non-drive-letter absolute path (finding 4):
//   `/Users/*/notes.md` typed on a machine with no `/Users` produces the
//   guessed folder `/Users`, and `navigate` there lands on a folder that
//   does not exist — an error listing, and the query is gone. The server
//   resolves this exact ambiguity by trying the absolute walk first and
//   falling back to the box's own root when that walk cannot even consume
//   its first segment (`resolve_query`'s own docstring) — again, a real
//   filesystem check this function is not in a position to make.
//
// Both return null here, same as the `home === undefined` case above:
// nothing this function can responsibly navigate to yet. The caller
// (SearchField.tsx) already has a fallback for null — `commitSearch()`,
// which leaves the box rooted where it is and sends the query text
// unchanged to the same `resolve_query` that gets both of these right. A
// `~`-relative folder keeps navigating (unchanged from ITEM 10): it is the
// one shape this function can expand with no ambiguity and no filesystem
// access at all, so it stays the one case worth the (already-documented,
// already-accepted) risk that the named folder itself does not exist yet.
export function resolveFolderToOpen(query: string, home: string | undefined): string | null {
  if (query.split("/").includes("..")) return null;
  const folder = folderToOpen(query);
  if (folder === null) return null;
  if (folder === "~" || folder.startsWith("~/")) {
    if (home === undefined) return null;
    return home + folder.slice(1);
  }
  if (folder.startsWith("/") && !/^[A-Za-z]:[\\/]/.test(folder)) return null;
  return folder;
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
