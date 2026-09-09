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
// field's queries are implicitly scoped to.
// The caller still has to `statPath` it — this function only says what to
// ask about, not whether it exists.
// A Windows drive-letter path (`C:\` or `C:/`) — same test query.py's
// `_DRIVE_ABS` runs server-side, and the same shape home-search.ts's
// `pathShortcut` already accepts for the home box.
const DRIVE_ABS = /^[A-Za-z]:[\\/]/;

export function listingAddress(
  query: string,
  fsPath: string,
  home: string | undefined,
): string | null {
  const raw = query.trim();
  if (!raw || raw.includes("*")) return null;
  if (
    !raw.includes("/") &&
    raw !== "~" &&
    !raw.startsWith("~/") &&
    !DRIVE_ABS.test(raw)
  ) {
    return null;
  }

  let path: string;
  if (raw === "~" || raw.startsWith("~/")) {
    if (home === undefined) return null;
    path = home + raw.slice(1);
  } else if (DRIVE_ABS.test(raw)) {
    // Backslashes are only separators here — on POSIX "\" is a legal
    // filename char, but a drive-letter path is never POSIX.
    path = raw.replace(/\\/g, "/");
  } else if (raw.startsWith("/")) {
    path = raw;
  } else {
    path = fsPath.replace(/\/+$/, "") + "/" + raw;
  }
  path = path.replace(/\/+$/, "");
  if (!path) return "/";
  // A bare drive root reads as cwd-relative without its slash — keep it
  // whole, the same rule home-search.ts's `pathShortcut` applies.
  if (/^[A-Za-z]:$/.test(path)) path += "/";
  return path;
}
