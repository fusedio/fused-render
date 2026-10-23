// Decision 4's gate, restated: Enter is required only when the query's base
// can differ from the box being searched. `completion-target.ts` is the
// sibling that fully resolves a path-shaped query (dir + partial, walking
// `~` and drive letters out to a real path); this predicate answers a much
// narrower yes/no question and, on purpose, needs neither `fsPath` nor
// `home` to answer it — a relative query's base is always the box's own
// root, whatever that root happens to be.
//
// A query escapes the box root when it starts with `~` (alone or `~/`),
// starts with `/`, matches a drive letter (`C:/`, `C:\`), or contains a `..`
// segment. Everything else — including a glob with slashes in it, like
// `*/*.json` — stays anchored at the box root and does not escape: a slash
// only limits how deep the pattern reaches, it does not relocate the base.
//
// A leading `/` is treated as escaping even though it is sometimes just a
// depth-1 anchor at the box root (`/*.csv`) rather than an absolute path
// (`/etc/*/x.conf`). The server resolves that ambiguity by walking the
// filesystem (fused_render/index/query.py), which this predicate cannot do
// synchronously and does not attempt to. Treating every leading `/` as
// escaping is the safe side of the ambiguity: a genuine absolute path is
// the expensive case and it does get gated, while `/foo` used as an anchor
// only costs one extra keypress.
import { expandWhitespaceQuery } from "@apps/explorer/lib/home-search";

const DRIVE_ABS = /^[A-Za-z]:[\\/]/;

export function escapesBase(query: string): boolean {
  if (query === "~" || query.startsWith("~/")) return true;
  if (query.startsWith("/")) return true;
  if (DRIVE_ABS.test(query)) return true;
  // Segments, not a substring: a file named `..config` is not `..`.
  return query.split("/").includes("..");
}

// SPEC-omnibox-search-affordance.md correction (2026-09-10): `escapesBase`
// above answers "could this query's base be anywhere other than the box's
// own root" from SHAPE alone, with no `fsPath` — exactly what
// `isPathShapedQuery` (path-shaped-query.ts) needs, and it stays exactly as
// it is; changing its meaning would flip which queries read as "Path" at
// all, which is not this predicate's job.
//
// The commit gate (useListingSearch.ts's `escapes`) and the dropdown's
// search-offer row (search-action-rows.ts) both ask a NARROWER question
// `escapesBase` alone cannot answer without `fsPath`: is this query's base
// still the SAME folder actually being searched, or a genuinely different
// one? `escapesBase` treats EVERY leading "/" as escaping, even one that
// resolves right back inside the folder already open — and the box always
// arrives pre-filled with that folder's own absolute path (a hard
// requirement), so appending a pattern to what is already sitting there is
// the single most natural gesture this box offers, and `escapesBase` alone
// sends exactly that gesture down the slower, gated route.
// `/Users/iamsdas/*/*.json` typed while standing in `/Users/iamsdas` is the
// same search as `*/*.json` typed there — same base, same scope — and
// should behave identically: live hits, no gate, no offer to press Enter
// for something already on screen.
//
// A genuine escape — a base outside the folder being searched — keeps the
// gate: changing which subtree gets walked is a real scope change, and an
// explicit Enter is what confirms it.
// FINDING (code review round 3, worktree-search-trailing-space): a plain
// `.trim()` (the round-1 fix) was wrong because it treats edge whitespace as
// meaningless — but trailing whitespace is NOT meaningless to the server.
// `expand_whitespace_query` (fused_render/index/query.py) turns a trailing
// space into a wildcard on the FINAL path segment, which can peel that
// segment off the walked base entirely: `/Users/iamsdas ` (the box's own
// pre-filled path plus one trailing space) resolves server-side to `base:
// "/Users", pattern: "**iamsdas**"` — the walk stops at the PARENT, because
// "iamsdas**" now carries a glob character and `_walk_from` cannot consume a
// glob-bearing segment as a real directory. A trim-based comparison sees
// "iamsdas" === "iamsdas" and calls that "still inside the folder" — exactly
// the scope change this predicate exists to catch, missed. Verified live
// against `/api/index/rank`: `q=/Users/iamsdas%20` answers `base: "/Users"`,
// not `/Users/iamsdas`.
//
// Symmetrically, a trailing space defeats `escapesBase`'s "~"/"~/"
// exact-match checks in the OTHER direction: `expand_whitespace_query` wraps
// a lone "~" plus trailing space into a glob token (`"**~**"`) that no
// longer starts with a literal "~" at all, so the server does not resolve it
// as a home path — it searches the CURRENT folder recursively for the
// literal character "~". Verified live: `q=~%20` (root `/Users/iamsdas`)
// answers `base: "/Users/iamsdas", pattern: "**/**~**"` — the box's own
// root, never `home`.
//
// This mirrors the SAME two-step process `resolve_query` itself runs
// (fused_render/index/query.py) — strip a LEADING run of whitespace only
// when what is left already looks like one of the escape forms (a lone
// leading space carries no meaning for that grammar and would otherwise be
// swallowed into a `**` token glued onto the very prefix being looked for),
// then run the REAL whitespace-expansion transform (`expandWhitespaceQuery`,
// lib/home-search.ts, byte-equivalent with `expand_whitespace_query`) —
// never a bare trim. A query with no whitespace and no "*" is a no-op under
// that transform, so every existing edge-free case is unaffected; only a
// query whose whitespace/glob shape actually changes what the server
// searches changes verdict here too.
//
// EXPORTED (round 3): `isPathShapedQuery` (path-shaped-query.ts) used to run
// its own bare `.trim()` before `escapesBase`, which regressed to exactly
// finding 1's bug from the OTHER caller — a trailing space on a folder path
// still read as "Path" (suppressing search entirely) because the trim threw
// away the same glob-injection this function now accounts for. Both callers
// now share this ONE normalization step so they cannot diverge from each
// other, or from the server, again.
export function normalizeQueryForResolution(rawQuery: string): string {
  const lstripped = rawQuery.replace(/^\s+/, "");
  let normalized = rawQuery;
  if (
    lstripped !== rawQuery &&
    (lstripped === "~" ||
      lstripped.startsWith("~/") ||
      lstripped.startsWith("/") ||
      DRIVE_ABS.test(lstripped) ||
      lstripped.split("/").includes(".."))
  ) {
    normalized = lstripped;
  }
  return expandWhitespaceQuery(normalized);
}

export function escapesFsPath(
  rawQuery: string,
  fsPath: string,
  home: string | undefined,
): boolean {
  const query = normalizeQueryForResolution(rawQuery);
  if (!escapesBase(query)) return false;
  // A ".." segment always walks up and out of `fsPath` — genuinely a
  // different subtree no matter where it lands — so no further check is
  // needed for it. (Whether it could theoretically re-descend into the same
  // subtree, e.g. "a/../a/*.json", is not a case finding 2 asked about, and
  // treating it as escaping matches `escapesBase`'s own existing verdict.)
  if (query.split("/").includes("..")) return true;

  // FINDING 4 (code review, 2026-09-10): a leading "/" is genuinely
  // ambiguous, and the server (`resolve_query`'s own docstring,
  // fused_render/index/query.py) resolves that ambiguity by trying the
  // whole thing as an absolute path first, falling back to a depth-1
  // anchor at the box's own root ONLY when that walk cannot even consume
  // its first segment (`_walk_from`'s `advanced` flag false — no real
  // directory backs it, or the first segment is itself glob-bearing).
  // `/*.csv` and a bare `/` both hit that fallback unconditionally — the
  // walk never even enters its loop — so this predicate can match the
  // server exactly for THOSE two shapes with no filesystem access of its
  // own. A literal first segment (`/etc/...`) is left on the gated side:
  // whether the server's fallback fires there depends on whether that
  // directory actually exists, which this predicate cannot check, and the
  // safe side of an unresolvable ambiguity is the gate (same bias
  // `escapesBase` already takes for every leading "/").
  const isBareSlash = query.startsWith("/") && !DRIVE_ABS.test(query);

  let abs: string;
  if (query === "~" || query.startsWith("~/")) {
    // Home not resolved yet: nothing to compare against, so stay on the
    // safe (gated) side rather than guess.
    if (home === undefined) return true;
    abs = home + query.slice(1);
  } else {
    // A leading "/" or a drive letter — backslashes are only separators on
    // a drive-letter path (completion-target.ts's `completionTarget` makes
    // the same normalisation for the same reason).
    abs = query.replace(/\\/g, "/");
  }

  // The query's own BASE: the segments before its first glob-bearing one,
  // or every segment when there is no glob at all. Deliberately NOT
  // enter-prompt.ts's `folderToOpen`, which drops a trailing non-glob
  // segment as a name pattern to filter by — that reasoning is about what
  // folder Enter would OPEN, a different question from what subtree this
  // predicate is asking about: a full, glob-free address names an exact
  // folder, not "a folder plus a filter word".
  const segments = abs.split("/").filter(Boolean);
  const globIdx = segments.findIndex((s) => /[*?]/.test(s));
  const baseSegments = globIdx === -1 ? segments : segments.slice(0, globIdx);

  // The bare-slash fallback above, made concrete: zero base segments means
  // the walk from "/" never advanced past root at all — a bare `/` (no
  // segments) or a `/`-prefixed query whose very first segment already
  // carries the glob (`/*.csv`). Both are certain, not a guess, so this
  // resolves to the box's own root — not an escape — exactly like the
  // equivalent query with the leading "/" dropped.
  if (isBareSlash && baseSegments.length === 0) return false;

  // "Inside, or exactly, the folder being searched": every segment of
  // `fsPath` has to appear, in order, at the START of the query's base — a
  // SEGMENT comparison, not a string prefix, so "/Users/iamsdas2" is never
  // mistaken for something inside "/Users/iamsdas".
  const fsSegments = fsPath.split("/").filter(Boolean);
  for (let i = 0; i < fsSegments.length; i++) {
    if (baseSegments[i] !== fsSegments[i]) return true;
  }
  return false;
}
