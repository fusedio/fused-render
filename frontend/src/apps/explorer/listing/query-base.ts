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
export function escapesFsPath(
  query: string,
  fsPath: string,
  home: string | undefined,
): boolean {
  if (!escapesBase(query)) return false;
  // A ".." segment always walks up and out of `fsPath` — genuinely a
  // different subtree no matter where it lands — so no further check is
  // needed for it. (Whether it could theoretically re-descend into the same
  // subtree, e.g. "a/../a/*.json", is not a case finding 2 asked about, and
  // treating it as escaping matches `escapesBase`'s own existing verdict.)
  if (query.split("/").includes("..")) return true;

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
