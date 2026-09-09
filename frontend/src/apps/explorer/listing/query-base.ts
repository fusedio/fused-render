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
