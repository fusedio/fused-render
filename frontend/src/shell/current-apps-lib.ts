// The sidebar's "Current apps" section (D487, redesigned 2026-08-26): the apps
// on the user's desk, read from a STORE of their own (`GET /api/current-apps`,
// fused_render/current_apps.py) rather than derived from the task pulse.
//
// The rules live server-side and are worth restating here because this file
// used to hold the old ones: a NEW task adds its app to the table; nothing
// removes one automatically (archive every task and the row stays); the row's
// cross removes the app AND archives every task under it. Every kind of app
// counts — any workspace folder with a declared page (`~/Fused/local`,
// `~/Fused/showcase`, …) and linked apps anywhere on disk (the registered-apps
// registry). What this file keeps is the CLIENT half: the row shape, the
// containment test the app page's Tasks tab applies, the page's URL codec, and
// the drag-ordered sequence layer.
//
// EVERY app is rendered — the list is not capped (owner, 2026-08-26).
import type { CurrentAppEntry } from "@platform/lib/api";

// The explorer's fs-path codec (router.ts encodeFsPathSegments / rootedFsPath),
// restated here rather than imported: router.ts rewrites `location` at import
// and this lib must stay DOM-free for its bun tests. Same rules — only a
// drive-letter path has its backslashes folded, a bare drive re-roots to `C:/`.
function encodeFsPathSegments(fsPath: string): string {
  const norm = /^[A-Za-z]:[\\/]/.test(fsPath) ? fsPath.replace(/\\/g, "/") : fsPath;
  return norm
    .replace(/^\/+/, "")
    .split("/")
    .filter((s) => s.length > 0)
    .map(encodeURIComponent)
    .join("/");
}

function rootedFsPath(joined: string): string {
  if (/^[A-Za-z]:$/.test(joined)) return joined + "/";
  return /^[A-Za-z]:\//.test(joined) ? joined : "/" + joined;
}

export interface CurrentApp {
  /** Absolute app folder, forward-slash (canonical server form) — the key. */
  path: string;
  /** The folder name — the row's label. */
  name: string;
  /** The page to run (canonical path), or null when the folder has none —
   *  the context menu's "Open app", the same target as the app page's own
   *  "Open app" button. */
  entry: string | null;
  /** `linked` for a registry folder outside the workspace, else `workspace`. */
  kind: "workspace" | "linked";
  /** The folder is still on disk. A missing one still lists — removing it is
   *  the user's gesture, not the table's. */
  exists: boolean;
  /** Something is running under it right now — the row wears the running dot.
   *  Read from the task pulse, which the sidebar already subscribes to. */
  running: boolean;
  /** A task under it finished with something unread — the row wears the green
   *  dot (unless running: yellow outranks green, the Tasks row's own rule). */
  unread: boolean;
  /** The app's optional `icon.svg`, as a drawable URL (api.appIconUrl), or
   *  null — the glyph slot falls back to the generic mark. */
  iconUrl: string | null;
}

/** Is `project` this app's folder or somewhere inside it — the scope test the
 *  app page's Tasks tab applies (a task on a subfolder still belongs to the
 *  app). Exact prefix on the folder boundary, so `foo` never claims `foo2`. */
export function isUnderDir(project: string, dir: string): boolean {
  return project === dir || project.startsWith(dir + "/");
}

/** The store's rows as sidebar rows, in the store's ADDED order (oldest
 *  first), with the running dot read off the projects of the tasks currently
 *  in progress. */
export function currentApps(
  entries: CurrentAppEntry[],
  runningProjects: Iterable<string>,
  unreadProjects: Iterable<string> = [],
): CurrentApp[] {
  const live = [...runningProjects];
  const fresh = [...unreadProjects];
  return entries.map((e) => ({
    path: e.path,
    name: e.name,
    entry: e.entry,
    kind: e.kind,
    exists: e.exists,
    running: live.some((p) => isUnderDir(p, e.path)),
    unread: fresh.some((p) => isUnderDir(p, e.path)),
    iconUrl: e.icon ? iconUrlFor(e.icon, e.icon_mtime) : null,
  }));
}

/** api.ts `appIconUrl` restated (raw file + mtime cache key) — that module is
 *  not importable here for the same DOM-free reason as the codec above. */
function iconUrlFor(icon: string, mtime?: number | null): string {
  // The FULL float mtime as the cache key, not the floored second: picking a
  // new icon twice inside one second must still change the URL, or the row
  // keeps serving the browser's cached previous emoji (Bugbot, 2026-08-31).
  return (
    "/api/fs/raw?path=" + encodeURIComponent(icon) + (mtime ? "&v=" + mtime : "")
  );
}

// ---- the app page's address (D488) ------------------------------------------
//
// `/apps/<folder path>?_tab=<tab>` — the folder as path segments in the
// explorer's own codec (router.ts `encodeFsPathSegments` / `rootedFsPath`, so
// a Windows drive rides as `C:/…` exactly as it does under /explorer/view),
// and the tab as the `_tab` QUERY param, absent for the default. It was
// `/apps/<slug>/<tab>` — one folder name under <fused_dir>/local, tab as the
// last segment — until 2026-08-26, when the desk widened to every app kind and
// the address had to carry the whole folder. With the folder in the path a
// trailing tab segment is ambiguous (a folder itself named `tasks`), so the
// owner moved the tab back to the query the same day. `_tab` is underscored
// like the explorer's own `_mode`, so it cannot collide with a tab's own
// params (`?view=` on Tasks, `?file=` on Files), which ride across a switch
// untouched. shell.py serves the shell for everything under /apps/.

export const APP_PAGE_PREFIX = "/apps/";
/** Tab-strip order; the first is the default. THE list: the type below is
 *  derived from it, and AppPage.tsx's `TAB_DEFS` is a `Record` over that type,
 *  so adding a tab is one string here plus one entry there — the compiler
 *  refuses the second being forgotten. Not every tab is offered on every
 *  folder: `git` shows only inside a work tree (AppPage's `visibleTabs`), so
 *  the ROUTE knows six tabs while the strip may draw five. */
export const APP_PAGE_TABS = ["overview", "tasks", "files", "api", "git"] as const;
export type AppPageTab = (typeof APP_PAGE_TABS)[number];
export const DEFAULT_APP_PAGE_TAB: AppPageTab = APP_PAGE_TABS[0];

/** The query param that names the tab. Absent = the default tab. */
export const APP_PAGE_TAB_PARAM = "_tab";

function isTab(seg: string | null): seg is AppPageTab {
  return seg !== null && (APP_PAGE_TABS as readonly string[]).includes(seg);
}

/** The page URL for an app folder and tab. `search` is the current query to
 *  carry (a tab's own params — `?view=` on Tasks, `?file=` on Files — ride
 *  across a switch untouched); only `_tab` is rewritten, and it is DROPPED for
 *  the default so the default tab has exactly one address. */
export function appPageUrl(
  dir: string,
  tab: AppPageTab = DEFAULT_APP_PAGE_TAB,
  search = "",
): string {
  const params = new URLSearchParams(search);
  if (tab === DEFAULT_APP_PAGE_TAB) params.delete(APP_PAGE_TAB_PARAM);
  else params.set(APP_PAGE_TAB_PARAM, tab);
  const query = params.toString();
  return (
    APP_PAGE_PREFIX + encodeFsPathSegments(dir) + (query ? "?" + query : "")
  );
}

/** The app folder an app-page pathname names (canonical, forward-slash), or
 *  null for anything that is not one — every segment under /apps/ is the
 *  folder. Validated AFTER decoding: the folder is stat'ed and framed, and the
 *  server route is a bare shell fallback, so this is the only guard against
 *  `.`/`..` segments, an empty folder, or a malformed percent-escape. */
export function appPathFromPath(pathname: string): string | null {
  if (!pathname.startsWith(APP_PAGE_PREFIX)) return null;
  const raw = pathname
    .slice(APP_PAGE_PREFIX.length)
    .split("/")
    .filter((s) => s.length > 0);
  if (!raw.length) return null;
  let segments: string[];
  try {
    segments = raw.map(decodeURIComponent);
  } catch {
    return null;
  }
  for (const seg of segments) {
    if (!seg || seg === "." || seg === ".." || seg.includes("\\")) return null;
  }
  return rootedFsPath(segments.join("/"));
}

/** The tab a query names. Missing or unknown falls back to the default
 *  SILENTLY (the /ai-models posture): a stale link opens the page, not an
 *  error. */
export function appPageTabFromSearch(search: string): AppPageTab {
  const tab = new URLSearchParams(search).get(APP_PAGE_TAB_PARAM);
  return isTab(tab) ? tab : DEFAULT_APP_PAGE_TAB;
}

// ---- the displayed order: a sequence per app --------------------------------
//
// Rows render by DESCENDING sequence number, and a sequence changes for exactly
// two reasons: an app that is not in the list gets one, and the user drags a
// row. The store's added order seeds the sequences and then stops mattering —
// this list is something the user reads and points at, and a list that
// reshuffles itself under the cursor cannot be pointed at.
//
// The store is a plain Map held at MODULE level by the section, hydrated from
// and written back to localStorage (`ORDER_KEY` there) — so the order the user
// dragged survives a reload and the next launch, per machine. The owner picked
// localStorage over a server-side pref: a synchronous read means no load race
// against the list fetch, and there is no endpoint, no file format and no
// multi-window write conflict to arbitrate. The cost is that it is per browser
// profile and does not follow the user to another machine.
//
// An app that LEAVES the list (removed by its cross) is FORGOTTEN — sequence and
// all — so if it comes back it comes back as new, at the top. Nothing removed is
// remembered; that is the owner's rule. It also bounds the store by
// construction: what is held, and what is saved, is the apps on the desk and
// nothing else.
//
// The prune is guarded on a non-empty input so a list that has not loaded yet
// cannot wipe the order.
//
// Pruning makes assignment NON-MONOTONE, which is dangerous for state two tabs
// share — pruning per-tab against a per-tab live set is a write loop waiting to
// happen (Bugbot, 2026-08-26, on exactly that). What makes it safe here is that
// the tabs do not race to describe the world: only a DRAG writes to the store
// (see CurrentAppsSection), and a drag is one user gesture, not a poll. A fetch
// changes the order on screen and saves nothing, so there is no second writer to
// disagree with.

/** app path -> sequence. Higher sorts earlier. */
export type AppOrder = Map<string, number>;

/** Give every app in `apps` a sequence, and forget every app that is gone.
 *  `apps` must arrive OLDEST-ADDED FIRST (the store's order): fresh apps are
 *  numbered from the first up, so the newest ends with the highest sequence
 *  and lands at the top. Idempotent — an app that already has a sequence keeps
 *  it, which is what makes this safe to call during a render.
 *
 *  The prune is why the store never needs a size limit: it holds the desk, and
 *  the desk is folders a person made by hand. */
export function assignSequences(order: AppOrder, apps: CurrentApp[]): void {
  if (!apps.length) return;
  const live = new Set(apps.map((a) => a.path));
  for (const path of [...order.keys()]) if (!live.has(path)) order.delete(path);
  const fresh = apps.filter((a) => !order.has(a.path));
  if (!fresh.length) return;
  let next = 0;
  for (const seq of order.values()) next = Math.max(next, seq);
  for (const app of fresh) order.set(app.path, ++next);
}

/** `apps` in display order. Sequences are assumed assigned; an app without one
 *  sorts last rather than throwing, so a caller that skipped `assignSequences`
 *  still gets a list. */
export function bySequence(apps: CurrentApp[], order: AppOrder): CurrentApp[] {
  return [...apps].sort(
    (a, b) => (order.get(b.path) ?? 0) - (order.get(a.path) ?? 0),
  );
}

/** Write `paths` (display order, top first) into the store as the new order.
 *  Renumbers the whole visible list from `paths.length` down to 1 in one go, so
 *  no drag can leave two rows sharing a sequence. */
export function reorderTo(order: AppOrder, paths: string[]): void {
  let seq = paths.length;
  for (const path of paths) order.set(path, seq--);
}

/** The store as a display-ordered path list — what a drag saves, and the input
 *  `reorderTo` takes back. Since the store is pruned to the apps on the desk,
 *  this is exactly the rows on screen: there is no remembered tail that could
 *  interleave itself into an order the user arranged by hand. */
export function orderedSlugs(order: AppOrder): string[] {
  return [...order.entries()].sort((a, b) => b[1] - a[1]).map(([p]) => p);
}

/** A saved order, read back. What came out of localStorage is a string written
 *  by SOMEONE ELSE (an older build, a hand-edited devtools row), so anything
 *  unreadable degrades to "no saved order" — the list seeds from the store's
 *  order, which is exactly where it started — rather than throwing inside a
 *  render. Non-string entries are dropped and duplicates collapse to their
 *  first appearance: two rows sharing a sequence would make the display order
 *  depend on sort stability, and that is not a thing to leave to a corrupt row. */
export function parseSavedOrder(raw: string | null): string[] {
  if (!raw) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return [];
  }
  if (!Array.isArray(parsed)) return [];
  const out: string[] = [];
  const seen = new Set<string>();
  for (const path of parsed) {
    if (typeof path !== "string" || !path || seen.has(path)) continue;
    seen.add(path);
    out.push(path);
  }
  return out;
}

/** `paths` with `from` lifted out and re-inserted at `to`'s slot — the list a
 *  drop produces. `below` puts it after the target rather than before. Returns
 *  the input untouched when the drag cannot move anything. */
export function moveSlug(
  paths: string[],
  from: string,
  to: string,
  below: boolean,
): string[] {
  if (from === to) return paths;
  const rest = paths.filter((s) => s !== from);
  const at = rest.indexOf(to);
  if (at === -1) return paths;
  rest.splice(at + (below ? 1 : 0), 0, from);
  return rest;
}
