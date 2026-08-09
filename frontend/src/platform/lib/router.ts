// fs-path <-> /explorer/view/ URL codec + navigation. UI-free. The vanilla
// shell registered a route() handler here; the React shell instead listens for
// the "fused:navigate" event (useNavEpoch in lib/hooks.ts) — navigate/
// navigateUrl dispatch it after pushState, popstate is subscribed alongside it.
export const VIEW_PREFIX = "/explorer/view/";

// Embed = chrome-free variant of view (same shell, same routing, just no
// sidebar/breadcrumb/preview-header). The mode is fixed at page load: both
// prefixes are served by full page loads, so it can't change without one.
export const EMBED_PREFIX = "/explorer/embed/";

// Pre-rename URL shapes (old bookmarks/recents entries, .bookmark files,
// external embed links). Settings sentinels became plain routes at the same
// time as the /explorer prefix rename.
const LEGACY_SENTINELS: Record<string, string> = {
  "/view/_home": "/apps",
  "/view/_prefs": "/preferences",
  "/view/_templates": "/templates",
  "/view/_mounts": "/mounts",
  "/view/_account": "/preferences",
};

// A legacy url mapped to its current shape; already-current urls pass through
// untouched. Needed in TWO places: at module init below (a full page load on a
// legacy url), and inside navigateUrl (a stored bookmark/recents url clicked
// IN-APP — preventDefault means no page load, so init never re-runs and the
// pushed path must already be current or routing won't recognize it).
export function rewriteLegacyUrl(url: string): string {
  const qIdx = url.indexOf("?");
  const p = qIdx === -1 ? url : url.slice(0, qIdx);
  const q = qIdx === -1 ? "" : url.slice(qIdx);
  if (p === "/view/_account") return "/preferences?tab=account";
  const mapped = LEGACY_SENTINELS[p];
  if (mapped) return mapped + q;
  if (p.startsWith("/view/") || p.startsWith("/embed/")) return "/explorer" + p + q;
  return url;
}

// Rewritten in place at module init — before IS_EMBED is computed, so a
// legacy /embed/ load still comes up in embed mode.
(function rewriteLegacyPath(): void {
  const current = location.pathname + location.search;
  const next = rewriteLegacyUrl(current);
  if (next !== current) history.replaceState(history.state, "", next);
})();

export const IS_EMBED =
  location.pathname.startsWith(EMBED_PREFIX) ||
  location.pathname === "/explorer/embed";

// A FROZEN TREE, not a live folder: the framing a view uses when it embeds a
// materialised historical snapshot (`versions`, which extracts a commit into
// ~/.fused-render/app-versions/<key>/<sha>/ and frames that directory).
//
// One flag for three symptoms of one cause, all of which are chrome that acts
// on the listing AS A LIVE FOLDER and has no meaning over a frozen copy:
//
//   * the breadcrumb, whose crumbs walk ABOVE the framed directory — straight
//     into the snapshot cache's own internals (`~ / .fused-render / branches /
//     … / app-versions / <hash> / <sha>`), a path the user never chose and
//     cannot act on;
//   * the "Browse contents" mode chip, which over a snapshot dir offers the
//     folder's counterpart mode — a Claude chat ON THE EXTRACTED COPY, which is
//     nonsense pointed at a frozen tree;
//   * the "Open as app" chip, same argument.
//
// A param and not a prefix (a third `/explorer/frozen/` route) because this is
// the SAME view of the same path — only its chrome differs — and the shell
// already carries exactly this kind of framing flag on this exact surface:
// `preview=false` (the listing's own pane) rides beside it, and `modechip=false`
// was its predecessor until D237's only producer went away, with the SPEC noting
// the opt-out "comes back with that caller". This is that caller.
//
// Read ONCE at module init, like IS_EMBED: both prefixes are served by full page
// loads, so the framing cannot change without one, and a value read per render
// would be a second source of truth for a fact that never moves.
export const IS_SNAPSHOT =
  new URLSearchParams(location.search).get("snapshot") === "1";
// URL prefix for this page's mode. Keeps refresh, in-listing navigation, and
// param sync (iframe runtime's history.replaceState) inside the active prefix.
const PREFIX = IS_EMBED ? EMBED_PREFIX : VIEW_PREFIX;

// App-builder namespace: /apps/<tag>/<name> — pretty URLs for app folders,
// which live exactly two levels under the workspace (<fused_dir>/<tag>/<name>,
// see fused_render/server/routers/apps.py). Pure codec against fused_dir; no
// server lookup. /apps itself (no segments) is the apps homepage, not an app.
export const APP_ROUTE_PREFIX = "/apps/";

export function appUrlForFsPath(fsPath: string, fusedDir: string): string | null {
  const root = fusedDir.replace(/\/+$/, "");
  if (!fsPath.startsWith(root + "/")) return null;
  const segs = fsPath.slice(root.length + 1).split("/").filter((s) => s.length > 0);
  if (segs.length !== 2) return null;
  return APP_ROUTE_PREFIX + segs.map(encodeURIComponent).join("/");
}

// The (tag, name) identity a builder-route pathname carries, or null when the
// pathname isn't one. Split out of fsPathFromAppRoute because the "linked"
// tag can't use the fused_dir codec at all — its folders live anywhere on
// disk, so the shell resolves that tag through the registry instead
// (GET /api/apps/linked-path, see App.tsx).
export function appRouteSegments(pathname: string): { tag: string; name: string } | null {
  if (!pathname.startsWith(APP_ROUTE_PREFIX)) return null;
  const segs = pathname
    .slice(APP_ROUTE_PREFIX.length)
    .split("/")
    .filter((s) => s.length > 0)
    .map(decodeURIComponent);
  if (segs.length !== 2) return null;
  return { tag: segs[0], name: segs[1] };
}

export function fsPathFromAppRoute(pathname: string, fusedDir: string): string | null {
  const segs = appRouteSegments(pathname);
  if (!segs) return null;
  return fusedDir.replace(/\/+$/, "") + "/" + segs.tag + "/" + segs.name;
}

export const NAV_EVENT = "fused:navigate";

function notifyNavigate(): void {
  window.dispatchEvent(new Event(NAV_EVENT));
}

// Windows fs paths are rooted at a drive letter ("C:/…"), not at "/" — the
// shell's canonical form keeps forward slashes and adds a leading slash only
// for POSIX paths. A bare drive ("C:", how a drive root decodes from a URL,
// whose segment split drops the trailing slash) canonicalizes to "C:/" —
// bare "C:" is cwd-relative for os.stat on Windows.
export function rootedFsPath(joined: string): string {
  if (/^[A-Za-z]:$/.test(joined)) return joined + "/";
  return /^[A-Za-z]:\//.test(joined) ? joined : "/" + joined;
}

export function fsPathFromLocation(): string | null {
  const p = location.pathname;
  if (!p.startsWith(PREFIX)) return null;
  const rest = p.slice(PREFIX.length);
  const decoded = rest
    .split("/")
    .filter((s) => s.length > 0)
    .map(decodeURIComponent)
    .join("/");
  return rootedFsPath(decoded);
}

export function urlForFsPath(fsPath: string, search?: string): string {
  // Windows callers (server stat/list results, bookmarks) may carry
  // backslashes; the URL codec speaks forward slashes only. Normalize ONLY
  // drive-letter paths — on POSIX a backslash is a legal filename character
  // and must round-trip untouched.
  const norm = /^[A-Za-z]:[\\/]/.test(fsPath) ? fsPath.replace(/\\/g, "/") : fsPath;
  const rest = norm.replace(/^\/+/, "");
  const encoded = rest
    .split("/")
    .filter((s) => s.length > 0)
    .map(encodeURIComponent)
    .join("/");
  return PREFIX + encoded + (search || "");
}

export function navigate(fsPath: string, opts?: { isDir?: boolean; mode?: string }): void {
  // Navigating between files/dirs drops old view params (fresh query string) —
  // EXCEPT `preview` (the listing's preview-pane visibility), which is sticky
  // across DIRECTORY navigation: the pane is workspace layout the user
  // toggled, so moving between folders must not silently close (or open) it.
  // Deliberately NOT carried onto file targets: `preview` is an unreserved
  // name, and the runtime's ancestor-climb (runtime.js D72 globals) makes
  // every shell-URL param readable from a template iframe — a file view
  // always hosts one, so carrying it there would shadow a user template's own
  // `preview` param. Going back (history) or re-entering a folder restores the
  // pane from that entry's URL / the folder's viewstate instead.
  const current = new URLSearchParams(location.search);
  const preview = current.get("preview");
  const parts: string[] = [];
  if (opts?.isDir === true && preview !== null) {
    parts.push("preview=" + encodeURIComponent(preview));
    // The pane's chosen mode (`_panelMode`) travels with the pane itself: a
    // folder hop with the pane open keeps previewing in the same mode.
    // Reserved (`_`-prefixed) name, so no template-param shadowing concern.
    const panelMode = current.get("_panelMode");
    if (panelMode !== null) parts.push("_panelMode=" + encodeURIComponent(panelMode));
  }
  // `opts.mode` picks the destination's template mode (`_mode`) — how the app
  // cards open a project folder straight into the plain app view (appEntry's
  // APP_OPEN_MODE) instead of the folder's file listing.
  if (opts?.mode) parts.push("_mode=" + encodeURIComponent(opts.mode));
  const search = parts.length ? "?" + parts.join("&") : "";
  // `opts.isDir` is a nav hint (the clicked listing row / breadcrumb already
  // knows whether the target is a directory): it rides in history.state so the
  // destination view can paint the right scaffold — a directory's listing plus
  // a template-strip spinner — BEFORE the ~1.6s stat resolves, instead of a
  // blank screen. Restored on back/forward (popstate carries the state), and
  // simply absent (null) for callers that don't know, which falls back to a
  // plain header scaffold. See navHintIsDir below.
  const state = opts && typeof opts.isDir === "boolean" ? { fsDir: opts.isDir } : null;
  history.pushState(state, "", urlForFsPath(fsPath, search));
  notifyNavigate();
}

// The directory hint carried by the navigation that landed on the current URL
// (see navigate). null = unknown: a fresh page load, a typed URL, or a caller
// that didn't pass one. Read once at the destination view's mount (StatView),
// which is why in-place param syncs must go through replaceSearch below — a
// raw history.replaceState(null, …) would wipe the hint off the current entry
// and Back/Forward to it would lose the scaffold.
export function navHintIsDir(): boolean | null {
  const s = history.state as { fsDir?: boolean } | null;
  return s && typeof s.fsDir === "boolean" ? s.fsDir : null;
}

// In-place view-param sync (sort/search/_mode/session replay) on the CURRENT
// history entry. Unlike navigate(), this MUST NOT create a history entry and
// MUST preserve the existing state — nulling it (a plain
// history.replaceState(null, …)) drops the { fsDir } hint navigate() stashed,
// so a later Back/Forward to this entry loses its directory scaffold and paints
// a blank/header-only view. Passing history.state through keeps the hint intact.
// Still routes through the main.tsx-wrapped replaceState, so fused:urlchange
// (bookmark buttons, hooks) fires exactly as before.
export function replaceSearch(url: string): void {
  history.replaceState(history.state, "", url);
}

export function navigateUrl(url: string, opts?: { isDir?: boolean }): void {
  // Like navigate(), but preserves the full url (incl. query string) — used
  // when opening a bookmark, whose url carries saved view params. Callers
  // that know the target's kind (e.g. Home's post-create hop into the app
  // folder's chat) pass the same isDir nav hint navigate() takes, so the
  // destination paints the right scaffold instead of the file one.
  const state = opts && typeof opts.isDir === "boolean" ? { fsDir: opts.isDir } : null;
  // Stored urls (bookmarks, recents, .bookmark files) may predate the
  // /explorer prefix rename; an in-app push skips the module-init rewrite, so
  // map here or the dispatcher won't recognize the path.
  history.pushState(state, "", rewriteLegacyUrl(url));
  notifyNavigate();
}

export function currentUrl(): string {
  return location.pathname + location.search;
}
