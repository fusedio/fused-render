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

// The stored string → the map, VALIDATED rather than cast. This is the only
// door into the store, so it is the one place that can honour the
// Record<string, string> the rest of the file is written against; it used to be
// a bare `JSON.parse(raw) as Record<string, string>`, and a cast checks nothing.
//
// What is on the other side of that door is a string written by someone else:
// an older build of this app, a foreign one sharing the origin, a hand-edited
// devtools value, a write interrupted half way. Three shapes have to survive:
//   • not JSON at all              → {}
//   • JSON but not an object       → {}. `null` is the one that used to get
//     through, because the guard was on the RAW string being non-empty and
//     "null" is not empty; `Object.entries(null)` throws.
//   • an object with junk INSIDE   → the junk entries are dropped and the rest
//     kept, because one bad key must not cost a user every folder's sort. A
//     non-string would otherwise reach `new URLSearchParams(42)`, which parses
//     the number's string form as a query — silently, and as nonsense.
//
// A hard failure here got much more expensive when purgeViewStateParams moved
// to pane.ts's MODULE INIT: the other callers are lazy and inside components,
// where a throw degrades one thing, but a throw at module init aborts a module
// that Listing.tsx and Preview.tsx both import — a blank app, not a blank pane.
// Exported for the tests, which have no localStorage to reach it through.
export function parseViewStateMap(raw: string | null): Record<string, string> {
  let parsed: unknown;
  try {
    parsed = raw ? JSON.parse(raw) : null;
  } catch {
    return {}; // malformed JSON — behave as empty
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
  const out: Record<string, string> = {};
  for (const [path, search] of Object.entries(parsed)) {
    if (typeof search === "string") out[path] = search;
  }
  return out;
}

function load(): Record<string, string> {
  try {
    return parseViewStateMap(localStorage.getItem(KEY));
  } catch {
    return {}; // private-mode / storage unavailable — behave as empty
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

// Drop a param from EVERY entry — the shape a retirement takes here. A key the
// app no longer reads is not harmless: it stays in storage forever, it is the
// thing a later reader misinterprets, and while it is there the old behaviour
// is one accidental read away from coming back.
//
// Pure, and separate from the storage below, for a testing reason: there is no
// localStorage in the bun suite, so the rewriting of every folder's query
// string — the part with rules (see the empty-entry case) — has to be reachable
// without one. Returns a new map; the caller decides whether to write it.
//
// An entry left with no params at all is DELETED rather than stored as "?" or
// "": setViewState already treats an empty search as absence, so keeping one
// would be an entry that says nothing and a map that only ever grows.
export function stripParam(map: Record<string, string>, name: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [path, search] of Object.entries(map)) {
    const s = new URLSearchParams(search);
    s.delete(name);
    const qs = s.toString();
    if (qs) out[path] = "?" + qs;
  }
  return out;
}

// The one-time purge itself: strip these params from the whole store, once, at
// the module init of whoever owned them. Cheap enough to run unconditionally on
// every load (one read, one write of a map that holds a query string per folder
// the user has visited), which is why there is no "have I already done this?"
// flag — a flag is a second piece of state that can itself go stale, and this
// migration is idempotent by construction.
export function purgeViewStateParams(...names: string[]): void {
  const map = load();
  let out = map;
  for (const name of names) out = stripParam(out, name);
  save(out);
}
