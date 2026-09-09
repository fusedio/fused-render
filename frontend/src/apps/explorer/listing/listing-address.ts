// Decision 5: whether a typed query in the listing's merged search field
// names one exact filesystem path, as opposed to a pattern to search for.
//
// A GLOB is never a candidate: "*" can match many paths at once, and there is
// no single one to stat. A query with no "/" and no leading "~" is a plain
// filter word (decision 3) and is never a candidate either — that is what
// keeps ordinary substring search from taking a round trip through `stat`
// on every keystroke.
//
// What remains — "~", "~/…", "/…", or a bare relative "a/b" — is resolved to
// an absolute path the same way `Breadcrumb.tsx`'s `submitEdit` resolves a
// typed path, with one addition: a relative query (no leading "/" or "~")
// resolves against the folder being searched, since that is the folder this
// field's queries are implicitly scoped to (`SPEC-one-search-language.md`).
// The caller still has to `statPath` it — this function only says what to
// ask about, not whether it exists.
export function listingAddress(
  query: string,
  fsPath: string,
  home: string | undefined,
): string | null {
  const raw = query.trim();
  if (!raw || raw.includes("*")) return null;
  if (!raw.includes("/") && raw !== "~" && !raw.startsWith("~/")) return null;

  let path: string;
  if (raw === "~" || raw.startsWith("~/")) {
    if (home === undefined) return null;
    path = home + raw.slice(1);
  } else if (raw.startsWith("/")) {
    path = raw;
  } else {
    path = fsPath.replace(/\/+$/, "") + "/" + raw;
  }
  path = path.replace(/\/+$/, "");
  return path || "/";
}
