// Per-path view state — currently the directory listing's sort (?sort/&order).
// A folder remembers how it was last viewed, so returning to it (clicking in,
// a breadcrumb, the browser Back button, or a fresh URL) restores that folder's
// own state rather than inheriting the previous view's. Keyed by canonical fs
// path; the value is a query string INCLUDING its leading "?" (or "" = none).
//
// This is deliberately a plain path->search store, not a param-carry across
// navigation: two sibling folders keep independent sorts (Desktop by Modified,
// fused by Size) and neither leaks into the other.
const KEY = "fused-render:viewstate";

function load(): Record<string, string> {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as Record<string, string>) : {};
  } catch {
    return {}; // private-mode / quota / malformed JSON — behave as empty
  }
}

function save(map: Record<string, string>): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(map));
  } catch {
    // storage unavailable — state is best-effort, so a failed write is fine
  }
}

// Saved search string for `path` ("" when nothing is stored). Carries a leading
// "?" so callers can hand it straight to urlForFsPath.
export function getViewState(path: string): string {
  return load()[path] || "";
}

// Persist (or, with an empty search, clear) the saved state for `path`.
export function setViewState(path: string, search: string): void {
  const map = load();
  if (search) map[path] = search;
  else delete map[path];
  save(map);
}
