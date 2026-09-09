// Same "~" contraction the top-bar Breadcrumb, the panel crumb strip, and the
// framed-panel crumb strip each compute inline (Breadcrumb.tsx,
// listing/path-crumbs.tsx, Panel.tsx) — strictly below home only, since home
// itself shows its full path, not a lone "~". This copy exists for the
// search-hit table's "Path in ~/…" header; the three existing call sites are
// untouched.
export function contractHome(fsPath: string, home: string | undefined): string {
  if (home !== undefined && fsPath.startsWith(home + "/")) {
    return "~" + fsPath.slice(home.length);
  }
  return fsPath;
}
