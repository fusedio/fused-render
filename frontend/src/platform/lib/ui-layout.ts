// The shell's per-machine LAYOUT document — sidebar width and fold, the two
// sidebar section folds, the explorer companion pane's shut / width / full
// screen, per-folder listing sort, the Tasks page's view / calendar range /
// board lanes / group folds, and the New task form's recent folders — persisted
// server-side at ~/.fused-render/ui_layout.json via GET/PUT /api/ui-layout
// (shell/ui_layout.py). Pure data layer: no DOM beyond a guarded localStorage
// read for the one-shot import, no React.
//
// WHY THE SERVER AND NOT localStorage (D1386). Every one of these used to be a
// `fused-render:*` localStorage key, and every one of them was silently lost on
// a relaunch of the desktop app: it walks ports 1777..1787 and then an
// ephemeral one (shell/onboarding.py, platform/lib/api.ts), and each port is a
// NEW ORIGIN with empty storage. So a sidebar dragged wide, a Board picked on
// Tasks, a folder sorted by size came back undone whenever the previous port was
// still held. The rule now: storage holds signals, caches and per-tab
// hand-offs; anything the user would miss across a relaunch lives behind an API.
//
// THE PATTERN is bookmarks.ts's, adapted for a document that changes on every
// drag:
//   • Reads are synchronous off an in-memory cache (`getUiLayout`), because
//     every consumer reads in a render or a `useState` initializer. The cache is
//     filled ONCE per document at boot (`hydrateUiLayout`, awaited by main.tsx
//     before the first render — see hydrate-ui-state.ts), never in an effect.
//   • Writes are `patchUiLayout(fn)`: applied to the cache synchronously (the
//     UI never waits on a PUT), then a debounced whole-document PUT.
//   • A failed PUT keeps the cache: the window goes on showing what the user
//     did, and the next patch tries again.
//
// WRITES REPLAY, THEY DO NOT CLOBBER. The server is whole-document
// last-write-wins (home_layout.py's posture), and this module is loaded by
// every shell window AND every embed iframe (each preview card on Home is one).
// A naive "PUT my cache" from a document hydrated a minute ago would put back
// that minute-old copy of every field it did not touch — one pane's side-width
// drag reverting another window's folder sort. So a flush is read-modify-write:
// GET the current document, replay this document's not-yet-persisted patch
// FUNCTIONS onto it, PUT the result. Which is why a patch is a function and why
// it must SET rather than TOGGLE: `d => ({...d, x: true})` replays to the same
// answer on any base, `d => ({...d, x: !d.x})` does not. Two documents writing
// the same field in the same instant is still last-write-wins; two documents
// writing different fields no longer lose either.
//
// THE ONE-SHOT IMPORT (D1386, owner Q2). On hydrate, every old localStorage key
// still present is read and turned into a patch, so nobody's layout resets on
// the upgrade; the key is removed once a flush carrying it has landed. The
// old-key literals live HERE rather than in each owner module because most of
// the owners (Scheduled, ScheduleCalendar, ScheduleTaskViews, NewJobModal) sit
// in the lazy Tasks chunk, which loads after hydrate — an import-time registry
// would miss exactly those. The parse is deliberately shape-only (a string, a
// boolean, an array of strings); each consumer still validates against its own
// vocabulary on read (TASK_VIEWS, BOARD_COLUMNS, MIN_W, initialRange), so a
// value this module admits is never trusted further than it was before.
// An import only fills a field the server document does NOT already have: a
// second origin (yesterday's port) still holding OLD values must not overwrite
// the layout the current one has been writing to the server since. Either way
// the old key is removed, so it cannot come back later.
import { getUiLayout as fetchUiLayout, putUiLayout, type UiLayoutDoc } from "@platform/lib/api";

export type { UiLayoutDoc };

// The document's own bounds, shared with shell/ui_layout.py's validator (which
// 400s anything outside them) and re-exported by sidebarstate.ts as the sidebar
// drag's limits — one set of numbers, stated where the document is defined.
export const SIDEBAR_WIDTH_MIN = 180;
export const SIDEBAR_WIDTH_MAX = 400;
export const SIDEBAR_WIDTH_DEFAULT = 232;
const VIEWSTATE_MAX_ENTRIES = 500;
const VIEWSTATE_MAX_KEY = 4096;
const VIEWSTATE_MAX_VALUE = 512;
const RECENT_PATHS_MAX = 8;
const TASKS_IDENT = /^[a-z0-9_-]{1,32}$/;

/** Debounce before the whole-document PUT: a resize drag or a burst of sort
 *  clicks is one write, not one per event. */
const FLUSH_DEBOUNCE_MS = 300;

type Patch = (d: UiLayoutDoc) => UiLayoutDoc;
type Obj = Record<string, unknown>;

const isObj = (v: unknown): v is Obj => !!v && typeof v === "object" && !Array.isArray(v);
const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
/** shell/ui_layout.py's `_is_key`: a non-empty string of at most 4096 chars.
 *  Applied to every free-form key and entry (viewState keys,
 *  recentPaths) — one entry failing it 400s the whole PUT
 *  (D1386). */
const isKey = (v: unknown): v is string => typeof v === "string" && v !== "" && v.length <= VIEWSTATE_MAX_KEY;
const clampInt = (n: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, Math.round(n)));

/**
 * Any value as a document the server will accept — the server's validation
 * (shell/ui_layout.py) mirrored field for field. Run on what the server sends,
 * on what an import produces and before every PUT, because ONE bad value 400s
 * the whole document, and a 400 here is silent: the user's every later layout
 * change would quietly stop persisting for the session. So out-of-range numbers
 * are clamped, junk entries are dropped, and the bounded collections keep their
 * NEWEST entries (viewState is appended to; recentPaths is
 * newest-first). Builds a fresh object field by field, so two equal documents
 * serialise identically (`sameDoc`).
 */
export function sanitizeUiLayout(raw: unknown): UiLayoutDoc {
  const src = isObj(raw) ? raw : {};
  const out: UiLayoutDoc = { v: 1 };

  const sb = src.sidebar;
  if (isObj(sb) && isNum(sb.width)) {
    out.sidebar = {
      width: clampInt(sb.width, SIDEBAR_WIDTH_MIN, SIDEBAR_WIDTH_MAX),
      collapsed: sb.collapsed === true,
    };
  }
  if (typeof src.bookmarksCollapsed === "boolean") out.bookmarksCollapsed = src.bookmarksCollapsed;
  if (typeof src.currentAppsCollapsed === "boolean") out.currentAppsCollapsed = src.currentAppsCollapsed;

  const es = src.explorerSide;
  if (isObj(es)) {
    const side: NonNullable<UiLayoutDoc["explorerSide"]> = {};
    if (typeof es.hidden === "boolean") side.hidden = es.hidden;
    if (es.width === null) side.width = null;
    else if (isNum(es.width) && es.width >= 0) side.width = Math.round(es.width);
    if (typeof es.maximized === "boolean") side.maximized = es.maximized;
    if (Object.keys(side).length) out.explorerSide = side;
  }

  if (isObj(src.viewState)) {
    const entries = Object.entries(src.viewState).filter(
      ([k, v]) =>
        isKey(k) &&
        typeof v === "string" &&
        v.length <= VIEWSTATE_MAX_VALUE &&
        (v === "" || v.startsWith("?"))
    ) as [string, string][];
    if (entries.length) out.viewState = Object.fromEntries(entries.slice(-VIEWSTATE_MAX_ENTRIES));
  }

  const t = src.tasks;
  if (isObj(t)) {
    const tasks: NonNullable<UiLayoutDoc["tasks"]> = {};
    if (typeof t.view === "string" && TASKS_IDENT.test(t.view)) tasks.view = t.view;
    if (typeof t.calRange === "string" && TASKS_IDENT.test(t.calRange)) tasks.calRange = t.calRange;
    if (Object.keys(tasks).length) out.tasks = tasks;
  }

  if (Array.isArray(src.recentPaths)) {
    const paths = [...new Set(src.recentPaths.filter(isKey))];
    out.recentPaths = paths.slice(0, RECENT_PATHS_MAX);
  }
  return out;
}

const sameDoc = (a: UiLayoutDoc, b: UiLayoutDoc) => JSON.stringify(a) === JSON.stringify(b);

function parseJson(raw: string): unknown {
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

const withTasks = (d: UiLayoutDoc, t: NonNullable<UiLayoutDoc["tasks"]>): UiLayoutDoc => ({
  ...d,
  tasks: { ...d.tasks, ...t },
});
const withSide = (d: UiLayoutDoc, s: NonNullable<UiLayoutDoc["explorerSide"]>): UiLayoutDoc => ({
  ...d,
  explorerSide: { ...d.explorerSide, ...s },
});

/**
 * Every retired localStorage key → how its stored string lands in the document.
 * Each `apply` FILLS AN ABSENT FIELD and otherwise returns `d` untouched (see
 * the header: a stale origin must not overwrite a live server document).
 * Shape-only parsing; `sanitizeUiLayout` and the owning store's read path do
 * the rest. The owners, for whoever greps an old key:
 *   sidebar                 platform/lib/sidebarstate.ts
 *   bookmarks-collapsed     apps/explorer/sidebar/BookmarksSection.tsx
 *   current-apps-collapsed  shell/CurrentAppsSection.tsx
 *   explorer-side-*         apps/explorer/lib/side-{hidden-,}store.ts, platform/lib/side-maximized-store.ts
 *   viewstate               platform/lib/viewstate.ts
 *   scheduled-view          shell/Scheduled.tsx
 *   scheduled-cal-range     shell/ScheduleCalendar.tsx, shell/TasksSkeleton.tsx
 *   recent-paths            shell/NewJobModal.tsx
 * (`fused-render:scheduled-view-tabs` is not here: the switcher's tabs are a
 * PREFERENCE, prefs.py `tasks_view_tabs`, and Scheduled.tsx imports it.)
 */
const LEGACY: ReadonlyArray<{ key: string; apply: (raw: string, d: UiLayoutDoc) => UiLayoutDoc }> = [
  {
    key: "fused-render:sidebar",
    apply: (raw, d) => {
      const p = parseJson(raw);
      if (d.sidebar || !isObj(p)) return d;
      return {
        ...d,
        sidebar: {
          width: isNum(p.width) ? p.width : SIDEBAR_WIDTH_DEFAULT,
          collapsed: p.collapsed === true,
        },
      };
    },
  },
  {
    key: "fused-render:bookmarks-collapsed",
    apply: (raw, d) => (d.bookmarksCollapsed !== undefined ? d : { ...d, bookmarksCollapsed: raw === "1" }),
  },
  {
    key: "fused-render:current-apps-collapsed",
    apply: (raw, d) => (d.currentAppsCollapsed !== undefined ? d : { ...d, currentAppsCollapsed: raw === "1" }),
  },
  {
    key: "fused-render:explorer-side-hidden",
    apply: (raw, d) => (d.explorerSide?.hidden !== undefined ? d : withSide(d, { hidden: raw === "1" })),
  },
  {
    key: "fused-render:explorer-side-width",
    apply: (raw, d) => {
      const px = Number(raw);
      if (d.explorerSide?.width !== undefined || raw === "" || !Number.isFinite(px)) return d;
      return withSide(d, { width: Math.max(0, Math.round(px)) });
    },
  },
  {
    key: "fused-render:explorer-side-maximized",
    apply: (raw, d) => (d.explorerSide?.maximized !== undefined ? d : withSide(d, { maximized: raw === "1" })),
  },
  {
    key: "fused-render:viewstate",
    apply: (raw, d) => {
      const p = parseJson(raw);
      return d.viewState || !isObj(p) ? d : { ...d, viewState: p as Record<string, string> };
    },
  },
  {
    key: "fused-render:scheduled-view",
    apply: (raw, d) => (d.tasks?.view !== undefined ? d : withTasks(d, { view: raw })),
  },
  {
    key: "fused-render:scheduled-cal-range",
    apply: (raw, d) => (d.tasks?.calRange !== undefined ? d : withTasks(d, { calRange: raw })),
  },
  {
    key: "fused-render:recent-paths",
    apply: (raw, d) => {
      const p = parseJson(raw);
      return d.recentPaths || !Array.isArray(p) ? d : { ...d, recentPaths: p as string[] };
    },
  },
];

// ---- state -----------------------------------------------------------------

let cache: UiLayoutDoc = { v: 1 };
/** True once this document has read the server's copy (hydrate, or the first
 *  successful flush when hydrate failed). Before that, reads are defaults plus
 *  this document's own patches. */
export let uiLayoutReady = false;
let hydration: Promise<void> | null = null;
/** Patches not yet on the server, in order. Replayed onto the server's copy at
 *  hydrate (so a patch made before a slow GET survives it) and at every flush. */
let queue: Patch[] = [];
/** The batch a flush has taken off `queue` and not yet landed: a hydrate GET
 *  that lands meanwhile must replay it too, or the cache loses those patches. */
let inflight: Patch[] = [];
/** The one-shot import's patches → the old key each carries. A key is removed
 *  from localStorage only by the flush that PUT its own patch (D1386): keyed by
 *  the patch rather than held as a loose set, so a batch the server refused
 *  cannot have its keys removed later by an unrelated flush that did land. */
const legacyOf = new Map<Patch, string>();
/** The server had no document: the next flush PUTs even when nothing changed. */
let mustCreate = false;
let timer: ReturnType<typeof setTimeout> | null = null;
let flushing = false;
let flushAgain = false;
/** Test seam: `_setUiLayoutForTest` turns the network off for good. */
let offline = false;
const listeners = new Set<() => void>();
const readyWaiters: Array<() => void> = [];

function applySafe(fn: Patch, d: UiLayoutDoc): UiLayoutDoc {
  try {
    return fn(d);
  } catch (e) {
    console.error("[fused] ui layout patch threw:", e);
    return d;
  }
}

/** The layout as this document knows it. Live object — treat as read-only and
 *  change it through `patchUiLayout`. */
export function getUiLayout(): UiLayoutDoc {
  return cache;
}

/**
 * Change the layout: `fn` is applied to the cache NOW (reads see it on the very
 * next line) and replayed onto the server's copy at the debounced flush. `fn`
 * must SET values, never toggle them relative to `d` (see the header), and must
 * return `d` itself when it changes nothing.
 */
export function patchUiLayout(fn: Patch): void {
  const next = applySafe(fn, cache);
  if (next === cache) {
    // Nothing to do against what this document holds — but before the server's
    // copy is in, "nothing" is only an answer about the defaults, so keep it
    // for the replay.
    if (!uiLayoutReady) queue.push(fn);
    return;
  }
  cache = next;
  queue.push(fn);
  scheduleFlush(FLUSH_DEBOUNCE_MS);
}

/**
 * Hear about the cache being REPLACED from the server — a hydrate that landed
 * after the boot's 2 s bound, when the first render already happened on
 * defaults. Not fired for `patchUiLayout`: the store that patched already knows.
 */
export function subscribeUiLayout(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/** Run `fn` once the server's copy is in (now, if it already is). For work that
 *  must see the REAL document rather than the defaults — a migration over every
 *  entry (viewstate's purge) run from a module's init, before hydrate. */
export function whenUiLayoutReady(fn: () => void): void {
  if (uiLayoutReady) fn();
  else readyWaiters.push(fn);
}

function adopt(server: UiLayoutDoc | null): void {
  let next = sanitizeUiLayout(server ?? { v: 1 });
  for (const fn of inflight.concat(queue)) next = applySafe(fn, next);
  next = sanitizeUiLayout(next);
  const changed = !sameDoc(next, cache);
  cache = next;
  uiLayoutReady = true;
  for (const fn of readyWaiters.splice(0)) {
    try {
      fn();
    } catch (e) {
      console.error("[fused] ui layout ready callback threw:", e);
    }
  }
  if (changed) listeners.forEach((fn) => fn());
}

function importLegacyKeys(): void {
  for (const { key, apply } of LEGACY) {
    let raw: string | null = null;
    try {
      raw = localStorage.getItem(key);
    } catch {
      return; // blocked storage: nothing to import, and nothing will be
    }
    if (raw === null) continue;
    const value = raw;
    const fn: Patch = (d) => apply(value, d);
    legacyOf.set(fn, key);
    patchUiLayout(fn);
  }
}

/**
 * Fill the cache from the server, once per document (idempotent). Runs the
 * one-shot import first, so even a GET that misses the boot bound renders with
 * the imported values. Never rejects: a failed GET leaves the defaults up and
 * the first later patch's flush tries the server again.
 */
export function hydrateUiLayout(): Promise<void> {
  if (hydration) return hydration;
  importLegacyKeys();
  hydration = (async () => {
    try {
      const { exists, layout } = await fetchUiLayout();
      // A flush that already read and wrote the server outranks this older GET.
      if (uiLayoutReady || offline) return;
      adopt(exists ? layout : null);
      if (!exists) mustCreate = true;
      if (mustCreate || queue.length) scheduleFlush(0);
    } catch (e) {
      console.error("[fused] failed to load the ui layout:", e);
    }
  })();
  return hydration;
}

function scheduleFlush(ms: number): void {
  // Nothing goes out before hydrate has at least started: a module-init patch
  // waits for the replay, and a bun test (which never hydrates) never fetches.
  if (!hydration || offline) return;
  if (timer !== null) clearTimeout(timer);
  timer = setTimeout(() => {
    timer = null;
    void flush();
  }, ms);
}

async function flush(): Promise<void> {
  if (flushing) {
    flushAgain = true;
    return;
  }
  const batch = queue;
  if (!batch.length && !mustCreate) return;
  queue = [];
  inflight = batch;
  flushing = true;
  let ok = false;
  try {
    // Read-modify-write (header): the base is the server's copy NOW, not this
    // document's possibly-stale one.
    const { exists, layout } = await fetchUiLayout();
    const base = sanitizeUiLayout(exists ? layout : null);
    let next = base;
    for (const fn of batch) next = applySafe(fn, next);
    next = sanitizeUiLayout(next);
    if (!exists || !sameDoc(next, base)) await putUiLayout(next);
    ok = true;
    mustCreate = false;
    for (const fn of batch) {
      const k = legacyOf.get(fn);
      if (k === undefined) continue;
      legacyOf.delete(fn);
      try {
        localStorage.removeItem(k);
      } catch {
        // blocked storage: the key cannot be removed, and cannot be read either
      }
    }
    // Hydrate never landed (it failed, or is still out): this read IS the
    // server's copy, so take it — with any patch made while it was in flight.
    if (!uiLayoutReady) adopt(next);
  } catch (e) {
    const status = (e as { status?: unknown } | null)?.status;
    if (typeof status === "number" && status >= 400 && status < 500) {
      // The server refused the document itself; retrying the same patches
      // would be refused the same way. Drop them (the cache keeps them) — but
      // LEAVE any old localStorage key they carried in place: its value never
      // landed, so the next load imports it again (D1386). Sanitize mirrors the
      // server's rules, so a refusal here means a bug, not bad data, and the
      // retry is what a fixed build gets to use.
      for (const fn of batch) legacyOf.delete(fn);
      console.error("[fused] ui layout rejected by the server:", e);
    } else {
      // Unreachable server: keep the patches for the next flush, which the
      // next patch schedules — no timer of our own retrying into a dead port.
      queue = batch.concat(queue);
      console.warn("[fused] ui layout not saved (will retry on the next change):", e);
    }
  } finally {
    inflight = [];
    flushing = false;
    if (flushAgain || (ok && queue.length)) {
      flushAgain = false;
      scheduleFlush(FLUSH_DEBOUNCE_MS);
    }
  }
}

/** Test seam: replace the cache (sanitised), mark it ready, drop every pending
 *  patch, and take this module offline so no test ever fetches. */
export function _setUiLayoutForTest(doc: UiLayoutDoc = { v: 1 }): void {
  offline = true;
  if (timer !== null) clearTimeout(timer);
  timer = null;
  queue = [];
  legacyOf.clear();
  mustCreate = false;
  cache = sanitizeUiLayout(doc);
  uiLayoutReady = true;
  readyWaiters.splice(0).forEach((fn) => fn());
}
