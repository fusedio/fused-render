// Per-path view state — currently the directory listing's sort (?sort/&order).
// A folder remembers how it was last viewed, so returning to it (clicking in,
// a breadcrumb, the browser Back button, or a fresh URL) restores that folder's
// own state rather than inheriting the previous view's. Keyed by canonical fs
// path; the value is a query string INCLUDING its leading "?" (or "" = none).
//
// This is deliberately a plain path->search store, not a param-carry across
// navigation: two sibling folders keep independent sorts (Desktop by Modified,
// fused by Size) and neither leaks into the other.
//
// WHERE IT LIVES: the `viewState` field of the server-held layout document
// (ui-layout.ts, D1384), read synchronously off that module's cache. It was the
// localStorage key `fused-render:viewstate` until the desktop app's port walk
// (a new origin, so empty storage, on most relaunches) was found resetting every
// folder's sort; ui-layout.ts imports that key once. Bounded there to the
// newest 500 folders (the server's cap), which is why `setViewState` moves a
// re-set folder to the end.
import { getUiLayout, patchUiLayout, whenUiLayoutReady } from "./ui-layout";

// A stored value → the map, VALIDATED rather than cast. This is the one door
// into the store, so it is the one place that can honour the
// Record<string, string> the rest of the file is written against.
//
// What is on the other side of that door was written by someone else: an older
// build of this app, the one-shot import of an old localStorage string, a
// hand-edited file. So it takes the old raw string as well as the document's
// object, and three shapes have to survive:
//   • not JSON at all              → {}
//   • JSON but not an object       → {}. `null` is the one that used to get
//     through, because the guard was on the RAW string being non-empty and
//     "null" is not empty; `Object.entries(null)` throws.
//   • an object with junk INSIDE   → the junk entries are dropped and the rest
//     kept, because one bad key must not cost a user every folder's sort. A
//     non-string would otherwise reach `new URLSearchParams(42)`, which parses
//     the number's string form as a query — silently, and as nonsense.
export function parseViewStateMap(raw: unknown): Record<string, string> {
  let parsed: unknown = raw;
  if (typeof raw === "string" || raw === null || raw === undefined) {
    try {
      parsed = raw ? JSON.parse(raw) : null;
    } catch {
      return {}; // malformed JSON — behave as empty
    }
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
  const out: Record<string, string> = {};
  for (const [path, search] of Object.entries(parsed)) {
    if (typeof search === "string") out[path] = search;
  }
  return out;
}

function load(): Record<string, string> {
  return parseViewStateMap(getUiLayout().viewState);
}

// Saved search string for `path` ("" when nothing is stored). Carries a leading
// "?" so callers can hand it straight to urlForFsPath.
export function getViewState(path: string): string {
  return load()[path] || "";
}

// Persist (or, with an empty search, clear) the saved state for `path`. The
// patch SETS this one folder's entry (ui-layout.ts replays it onto the server's
// copy, so another window's folders are kept); delete-then-add moves the folder
// to the end, which is the "newest" end the document's 500 cap keeps.
export function setViewState(path: string, search: string): void {
  if ((getUiLayout().viewState?.[path] ?? "") === search) return;
  patchUiLayout((d) => {
    const map = { ...d.viewState };
    delete map[path];
    if (search) map[path] = search;
    return { ...d, viewState: map };
  });
}

// Drop a param from EVERY entry — the shape a retirement takes here. A key the
// app no longer reads is not harmless: it stays in storage forever, it is the
// thing a later reader misinterprets, and while it is there the old behaviour
// is one accidental read away from coming back.
//
// Pure, and separate from the store below, for a testing reason: the bun suite
// has no server, so the rewriting of every folder's query string — the part
// with rules (see the empty-entry case) — has to be reachable without one.
// Returns a new map; the caller decides whether to write it.
//
// An entry left with no params at all is DELETED rather than stored as "?" or
// "": setViewState already treats an empty search as absence, so keeping one
// would be an entry that says nothing and a map that only ever grows.
//
// TWO THINGS ARE LEFT BYTE-IDENTICAL, and both matter to the caller:
//   • an entry that never carried the param keeps its exact stored string,
//     rather than being round-tripped through URLSearchParams. That round trip
//     normalizes the encoding (a space stored as `%20` comes back as `+`), and
//     while every reader here parses with URLSearchParams and cannot tell the
//     difference, a migration has no business making the STORED format depend
//     on that for entries it was not asked about.
//   • a map with nothing to strip comes back as THE SAME OBJECT. Identity is
//     how the caller below knows nothing changed, so it is load-bearing rather
//     than an optimisation.
export function stripParam(map: Record<string, string>, name: string): Record<string, string> {
  const out: Record<string, string> = {};
  let changed = false;
  for (const [path, search] of Object.entries(map)) {
    const s = new URLSearchParams(search);
    if (!s.has(name)) {
      out[path] = search;
      continue;
    }
    changed = true;
    s.delete(name);
    const qs = s.toString();
    if (qs) out[path] = "?" + qs;
  }
  return changed ? out : map;
}

// The one-time purge itself: strip these params from the whole store, at the
// module init of whoever owned them.
//
// There is no "have I already done this?" flag, and there should not be: a flag
// is a second piece of state that can itself go stale, and this migration is
// idempotent by construction — a store with nothing left to strip strips to
// itself. What that costs is one pass per document load, over a map holding a
// query string per folder the user has visited.
//
// The WRITE is the part that is conditional, on stripParam having actually
// removed something (which it signals by returning a different object). That is
// not the flag argued against above — it is a fact about this run, read from the
// data rather than remembered beside it.
//
// DEFERRED UNTIL THE LAYOUT IS IN (`whenUiLayoutReady`): the caller is a module
// init (listing/pane.ts), which runs before main.tsx's hydrate lands, and a
// purge over the not-yet-hydrated defaults would strip nothing — then the real
// map, imported legacy entries included, would arrive with the params intact.
export function purgeViewStateParams(...names: string[]): void {
  whenUiLayoutReady(() => {
    const map = load();
    let out = map;
    for (const name of names) out = stripParam(out, name);
    if (out === map) return;
    // Setting the whole map is right here, unlike setViewState: the purge is
    // re-run by every document, so a replay onto another window's copy strips
    // that copy too rather than putting this one's back.
    patchUiLayout((d) => {
      const base = parseViewStateMap(d.viewState);
      let next = base;
      for (const name of names) next = stripParam(next, name);
      return next === base ? d : { ...d, viewState: next };
    });
  });
}
