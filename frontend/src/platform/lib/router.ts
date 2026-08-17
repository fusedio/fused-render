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
  // A tab that MOVED PAGES, which the sentinel table cannot express: Inference
  // engines is the Engines tab of /ai-models now (shell/AiModelsEngines.tsx).
  // Without this, `?tab=engines` reaches a Preferences that no longer knows the
  // name and silently falls back to its default tab — a bookmark that looks
  // like the setting was deleted rather than moved. Applied to the MAPPED path
  // so the pre-rename `/view/_prefs?tab=engines` is caught by the same line.
  if ((mapped ?? p) === "/preferences" && new URLSearchParams(q).get("tab") === "engines") {
    return "/ai-models?tab=engines";
  }
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

// The param a display-only card peek stamps on its embed URL (BookmarkCards'
// LivePreview), and the flag GET /render takes to skip open recording (D301).
// One name end to end: the card marks the embed shell, the shell forwards it
// onto every /render URL it builds (Preview.tsx), the server skips the record.
export const PREVIEW_PARAM = "_preview";

// A same-origin ancestor frame carrying the thumbnail stamp. Inheritance is
// the point: a PREVIEWED page may itself embed other apps (the tutorial
// example iframes the sine app via /embed/ and the full shell via /view/),
// and those nested shells load with no flag of their own — so a card peek at
// the tutorial recorded opens of the apps INSIDE it. Any ancestor being a
// thumbnail makes this whole subtree a thumbnail, whatever prefix it loaded
// under. Cross-origin ancestors (or no DOM at all, in tests) read as "no".
function ancestorIsPreview(): boolean {
  try {
    let w: Window = window;
    while (w.parent && w.parent !== w) {
      w = w.parent;
      if (new URLSearchParams(w.location.search).get(PREVIEW_PARAM) === "1") return true;
    }
  } catch {
    /* cross-origin frame — not ours, so not our thumbnail */
  }
  return false;
}

// AM I A THUMBNAIL? Read once at module init like IS_EMBED — navigate() drops
// the query on every in-app hop, but a card peek never navigates (its pointer
// shield keeps every click on the card), so the load-time value is the truth
// for the document's whole life. Without this, a folder card peeking at an
// app's entry page RECORDS AN OPEN of that app every time the card scrolls
// into view, and the /apps recency order rearranges itself.
export const IS_PREVIEW =
  (IS_EMBED && new URLSearchParams(location.search).get(PREVIEW_PARAM) === "1") ||
  ancestorIsPreview();

// Mark an embed/render URL as a thumbnail. Idempotent (a bookmark's stored
// url may carry any query, and cards rebuild their src every render — an
// accumulating param would reload the frame for nothing), same shape as
// frame-focus's withNoFocus.
export function withPreviewFlag(src: string): string {
  if (new URLSearchParams(src.split("?")[1] ?? "").get(PREVIEW_PARAM) === "1") return src;
  return src + (src.includes("?") ? "&" : "?") + PREVIEW_PARAM + "=1";
}

// A FROZEN TREE, not a live folder: the framing a view uses when it embeds a
// materialised historical snapshot — a commit extracted into
// ~/.fused-render/app-versions/<key>/<sha>/ with that directory framed.
//
// NO VIEW WRITES OR FRAMES ONE ANY MORE: the per-path timeline mode that
// materialised these trees is gone, and the git view that replaced it renders a
// revision's bytes on read (/api/git/show) with nothing on disk. The flag stays
// because trees an older version left behind are still browsable by URL, and
// because it is the shell's one "you are being framed" bit (the fourth
// consequence below) — but it currently has no producer inside the app.
//
// One flag, and three of its four consequences are chrome that acts on the
// listing AS A LIVE FOLDER and has no meaning over a frozen copy:
//
//   * the breadcrumb, whose crumbs walk ABOVE the framed directory — straight
//     into the snapshot cache's own internals (`~ / .fused-render / branches /
//     … / app-versions / <hash> / <sha>`), a path the user never chose and
//     cannot act on;
//   * the "Browse contents" mode chip, which over a snapshot dir offers the
//     folder's counterpart mode — a Claude chat ON THE EXTRACTED COPY, which is
//     nonsense pointed at a frozen tree;
//   * the "Open as app" chip, same argument. (That chip is gone outright now —
//     D264 removed the app concept — but the flag still suppresses the two
//     above, which is the same reasoning applied to the survivors.)
//
// The fourth is about being FRAMED rather than about being frozen: the listing
// does not open a preview pane of its own (Listing.tsx). A snapshot is embedded
// in some view's column — the whole reason this flag exists — and a column wide
// enough to read is also wide enough for the listing's own split, so the
// browsable snapshot grew a second preview inside the first. It rides on this
// flag because a "you are being framed" bit would have exactly one writer, the
// same one, in exactly the same place.
//
// A param and not a prefix (a third `/explorer/frozen/` route) because this is
// the SAME view of the same path — only its chrome differs — and the shell
// already carries exactly this kind of framing flag on this exact surface:
// `modechip=false` was its predecessor until D237's only producer went away,
// with the SPEC noting the opt-out "comes back with that caller". This is that
// caller. (`preview=false`, the listing's own pane, used to ride beside it too;
// that one is gone as a PARAM — its job is the fourth consequence above, folded
// into this flag rather than kept as a second one nobody could write alone.)
//
// Read ONCE at module init, like IS_EMBED: both prefixes are served by full page
// loads, so the framing cannot change without one, and a value read per render
// would be a second source of truth for a fact that never moves.
export const IS_SNAPSHOT =
  new URLSearchParams(location.search).get("snapshot") === "1";

// Is this pathname panel mode's sentinel route? Both prefixes, because panel
// mode lives under the page's own one (Panel.tsx's PANEL_PATH) so that
// entering/refreshing/exiting stays in the active mode — which means the shell
// has to recognise either spelling. Exported so the two readers (App's route
// dispatch, and IS_PANEL_PANE below, which asks it of a HOST document) cannot
// drift into two spellings of one route.
export function isPanelPath(pathname: string): boolean {
  return pathname === VIEW_PREFIX + "_panel" || pathname === EMBED_PREFIX + "_panel";
}

// AM I A PANE OF A SPLIT? — the third framing flag, and the only one that is
// not a fact about this document's own URL.
//
// A panel pane is a whole shell loaded at `/explorer/embed/<path>`, so from the
// inside it looks exactly like a top-level window: it owns its bar chrome
// (`barChrome && !embedded` is true in there), it reflects sort/`_side` into its
// own address bar, it registers document-level keys. That is all deliberate —
// a pane IS a browsing context, and everything in it should behave. The one
// thing it must not do is grow the listing's own preview pane: the user already
// answered the layout question by splitting, and half of a window is not two
// readable columns. Exactly the argument Preview.tsx makes for the FILE
// sidebar's `splitCapable`.
//
// So why not `IS_EMBED`, which is what `splitCapable` uses? Because it is too
// coarse for THIS surface: it is also every TAB (Tabs.tsx frames the same
// /embed shells), and a tab is full-window — there is no split, nothing was
// answered, and its folder listing should keep the pane it has always had. It
// is also bookmark cards and any external embed. `splitCapable` can afford the
// looseness (a tab's file view genuinely doesn't want a second split either);
// a folder listing cannot.
//
// And the panes themselves carry NOTHING to tell the two apart: Panel and Tabs
// both build their iframe src through layout-codec's `embedSrc`, byte for byte
// identical. A marker param would be the obvious fix and is a trap — `navigate`
// deliberately drops the query on every hop (see there), so `?_pane=1` would
// survive exactly until the user clicked a folder inside the pane, and keeping
// it would mean adding a second exception beside `snapshot=1` to carry a bit
// the host already knows.
//
// So ASK THE HOST. Climbing to an ancestor's URL is the shell's existing
// same-origin idiom, not a new one: the template runtime reads its params off
// ancestor URLs the same way (D3/D4/D46), and panel/tab shells already reach
// down the other direction (readEmbedLoc reads a pane iframe's live location).
// The host's pathname is the route sentinel itself, so there is no flag for
// anyone to write, forget to write, or write inconsistently — one producer, and
// it is the route.
//
// Climbs the whole chain rather than checking `parent` alone: a listing can sit
// two frames deep inside a split (a pane showing the bookmarks page, whose
// cards are embeds of their own), and being three levels down in a pane is
// still being in a pane. `top` terminates it; the try/catch is for a
// cross-origin ancestor (an external embed), where the answer is "not a pane" —
// the same safe direction the flag's other cases point.
//
// Read ONCE at module init, like the two above, and for a stronger reason than
// theirs: a document cannot be re-parented into or out of a frame, so the fact
// physically cannot change without a fresh load of this document.
export const IS_PANEL_PANE = (function inPanelHost(): boolean {
  try {
    let win: Window = window;
    while (win !== win.parent) {
      win = win.parent;
      if (isPanelPath(win.location.pathname)) return true;
    }
  } catch {
    // Cross-origin ancestor: not our panel.
  }
  return false;
})();

// URL prefix for this page's mode. Keeps refresh, in-listing navigation, and
// param sync (iframe runtime's history.replaceState) inside the active prefix.
const PREFIX = IS_EMBED ? EMBED_PREFIX : VIEW_PREFIX;

// There was a SECOND URL namespace for app folders here — /apps/<tag>/<name>,
// a pretty route decoded against fused_dir (with the virtual "linked" tag
// resolved through the registry instead) that rendered the folder under the app
// builder's own chrome. It is gone, and no rewrite maps the old shape: an app
// folder is a directory like any other, and /explorer/view/<path> already names
// it. Two routes for one folder meant two answers to "where am I" — the
// breadcrumb, the sidebar and the mode switcher all differed by which one you
// arrived through — for a namespace whose only advantage was cosmetic.
//
// Old /apps/<tag>/<name> links are DROPPED rather than redirected (owner call),
// the same posture as `?_mode=versions` in D243: a stale link falls back to the
// shell's "Unrecognized URL", and a permanent alias would keep the dead shape
// alive in every bookmark and recents entry that has one.

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

export function navigate(
  fsPath: string,
  opts?: { isDir?: boolean; mode?: string; sel?: string | null },
): void {
  // Navigating between files/dirs drops old view params (fresh query string) —
  // EXCEPT the preview pane's own state (`_side`: which of its three modes it is
  // showing, or that it is shut — listing/pane-side.ts), which is sticky across
  // DIRECTORY navigation: a folder hop keeps the pane as the user left it, open on
  // the same companion or closed. Reserved (`_`-prefixed) name, so no
  // template-param shadowing concern, and directory-only for the same reason its
  // predecessor was: a file view hosts a template iframe whose ancestor-climb
  // (runtime.js D72 globals) reads every shell-URL param.
  //
  // It replaces `_panelMode`, which named which of the SELECTED ROW's templates
  // the pane was previewing. That switcher is gone (pane-side.ts records the
  // trade), so nothing writes the param and nothing would read one carried here.
  //
  // Its companion `preview` (the pane's on/off) is GONE, not merely unlisted:
  // the split is decided by the container's width now (listing/pane.ts), so
  // there is no visibility to carry between folders and nothing a stale param
  // could contradict. The `?sel=` selection param is likewise not CARRIED —
  // a name from the folder you LEFT names nothing in the folder you arrive in
  // (see useListingSelection) — but a caller may SET one for the destination
  // via `opts.sel`, which is how an upward hop lands with the folder you came
  // out of highlighted (listing/selection.ts cameFromSelParam). Relative to
  // the destination, exactly like the value the listing writes back.
  //
  // `snapshot=1` is the exception to the fresh-query-string rule, and it is a
  // different KIND of param from the two below: it says what this PAGE is — a
  // frozen tree framed in some view's column (see IS_SNAPSHOT) — not how the
  // destination should be viewed. Every hop the framed listing makes is still
  // inside that snapshot, so dropping it would make the url describe a page
  // that does not exist. IS_SNAPSHOT is read once at boot, so the live session
  // survives the drop; a RELOAD or a copied link is where it bites, bringing
  // back the breadcrumb walking up into the snapshot cache's internals and the
  // preview pane inside the preview pane. Carried on FILE hops as well as
  // folder ones, unlike `_side`: the framed listing opens files too, and
  // the chrome the flag suppresses is the same chrome on a file view.
  const current = new URLSearchParams(location.search);
  const parts: string[] = [];
  if (current.get("snapshot") === "1") parts.push("snapshot=1");
  if (opts?.isDir === true) {
    const side = current.get("_side");
    if (side !== null) parts.push("_side=" + encodeURIComponent(side));
  }
  // `opts.mode` picks the destination's template mode (`_mode`) — a caller
  // that wants the destination opened in a specific view rather than its own
  // default (the preview pane's expand button carries the mode it is showing).
  // Its other producer, the explorer's "Open as app", is gone with the app
  // concept (D264).
  if (opts?.mode) parts.push("_mode=" + encodeURIComponent(opts.mode));
  if (opts?.sel) parts.push("sel=" + encodeURIComponent(opts.sel));
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
