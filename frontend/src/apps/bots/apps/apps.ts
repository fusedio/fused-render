// Apps (OpenBot src/apps.js, the non-React half): the apps root, the embed/open URLs, the host-params bridge, Copy
// state, appFromText, agoShort. The gallery/viewer live in AppsPanel.tsx/AppViewer.tsx, the thread card in
// AppCard.tsx, the side app in side.ts/SideApp.tsx (showAppBeside is re-exported here for the thread).
//
// URLs: `/render?path=<dir>/index.html` (fused-render's app frame; FusedBot's was `/embed?path=`), so
// every extra key is appended with `&`. A gallery thumbnail carries `_preview=1`, which makes the framed runtime its
// own params target (runtime.js findTarget), so twelve thumbnails never write onto this page's URL; the viewer, the
// side app and the inline card carry no `_preview`, so their params land on THIS page's URL exactly as in OpenBot.
import type { AppRef } from "../lib/api";
import { goChat, showToast } from "../state/store";
import { buildsRoot, rootReady, setBuildsRoot, useBuildsRoot } from "../builds/builds";
import { botsMounted } from "../lib/root";
import { appsHref, parseBotsPath, type BotsRoute } from "../routes";
import { navigateUrl } from "@platform/lib/router";

export { showAppBeside, sideAppBrowser, sideAppShow, closeSideApp } from "./side";

/** Anything that names an app: an /api/bot-apps row, an event's `app`, a parsed link. */
export interface AppLike { name?: string; folder?: string; dir: string; desc?: string; params?: string; tools?: unknown }

// ------------------------------------------------------------------ root ----
/** builds.js BUILDS_ROOT (<Fused workspace>/app, from /api/config). */
export const appsRoot = (): string => buildsRoot();
export const setAppsRoot = (r: string): void => setBuildsRoot(r);
/** Resolves once the root is known (empty when /api/config could not be read). */
export const ensureAppsRoot = (): Promise<string> => rootReady().then(buildsRoot);
/** The apps root for rendering (empty until loaded; starts the load). */
export const useAppsRoot = (): string => useBuildsRoot();

// ------------------------------------------------------------------ URLs ----
/** `params` (optional, a query string) is the app's own state; the embed forwards it to the app. */
export const appEmbedBase = (dir: string, params?: string): string =>
  `/render?path=${encodeURIComponent(dir + "/index.html")}${params ? "&" + params : ""}`;
/** Card thumbnail: its own params target (_preview), never steals focus, never opens things. */
export const appEmbed = (dir: string): string => `${appEmbedBase(dir)}&_preview=1&_nofocus=1&_noopen=1`;
export const appOpenUrl = (dir: string, params?: string): string =>
  `/render?path=${encodeURIComponent(dir + "/index.html")}${params ? "&" + params : ""}`;
/** An event's `app.params` is a query string, or (older events) an object. */
export const paramString = (p: AppRef["params"] | undefined | null): string =>
  typeof p === "string" ? p : p ? new URLSearchParams(p as Record<string, string>).toString() : "";

// ------------------------------------------------------------------ host params ----
// Inside this page an embedded app's fused.params are THIS page's URL params: the runtime syncs to the outermost
// same-origin window, not to the embed frame. So the app's live state = our params minus our own keys, and reopening
// an app at a state means setting those params here.
// `new` and `phone` are this page's own one-shots (?new=1, ?phone=1); `task` is the Tasks page's peek key
// (/bots/tasks?task=, routes.ts); `peek` is the shell's task side peek (shell/task-peek-store PEEK_PARAM), which can
// sit on /bots too. None is an app's state. (The selected bot left the query for the path on 2026-10-14.)
export const HOST_KEYS = new Set(["task", "new", "phone", "peek"]);
export const appParamOk = (k: string): boolean => !HOST_KEYS.has(k) && k !== "path" && !k.startsWith("_");

/** The app's live state: this page's query minus host keys, `path` and `_*`. */
export function appStateParams(search: string = typeof location === "undefined" ? "" : location.search): string {
  const q = new URLSearchParams();
  try { for (const [k, v] of new URLSearchParams(search)) if (appParamOk(k)) q.set(k, v); } catch { /* unreadable */ }
  return q.toString();
}

/** Write an app's params onto this page's URL (replaceState), REPLACING whatever app keys were there: the host URL
 *  holds one app's state at a time, and a key the previous app left behind would otherwise be read back as this
 *  app's state (the runtime syncs `fused.params` from this URL) and copied out by "Copy state". Host keys, `path`
 *  and `_*` are never touched either way. The shell's main.tsx wraps replaceState to dispatch `fused:urlchange`,
 *  which the runtime and the store follow. */
export function applyAppParams(params?: string | null): void {
  try {
    const u = new URL(location.href);
    for (const k of [...u.searchParams.keys()]) if (appParamOk(k)) u.searchParams.delete(k);
    for (const [k, v] of new URLSearchParams(params || "")) if (appParamOk(k)) u.searchParams.set(k, v);
    if (u.href !== location.href) history.replaceState(history.state, "", u.href);
  } catch { /* no history (tests) */ }
}

/** Drop every app key from this page's URL — the viewer or the side app closed and nothing shows an app any more. */
export function clearAppParams(): void { applyAppParams(""); }

/** Every surface that is currently showing an app whose live state is this URL's params: the side app and each
 *  expanded inline AppCard take a hold while their iframe runs and release it when it goes. The viewer is tracked by
 *  `viewerOpen` (AppViewer's mount). The params are cleared only when the LAST holder lets go, so closing one surface never wipes the
 *  `fused.params` of an app still running in another. (A Set of tokens, not a predicate: side.ts and AppCard import
 *  this module, so apps.ts cannot ask them.) */
const holders = new Set<symbol>();
export function holdAppParams(): () => void {
  const token = Symbol("app-params-hold");
  holders.add(token);
  return () => {
    if (!holders.delete(token)) return;
    if (!viewerOpen && holders.size === 0) clearAppParams();
  };
}
export const anyAppOpen = (): boolean => viewerOpen || holders.size > 0;

// "Copy state": a /render link carrying the app's current params. Paste it to any bot: the chat turns it into an app
// card that reopens the app exactly like this (appFromText below; _app_at in the backend for the bot's `show`).
export function copyAppState(a: AppLike | null | undefined): void {
  if (!a) return;
  const params = appStateParams(), url = location.origin + appOpenUrl(a.dir, params), ts = Date.now() / 1000;
  navigator.clipboard.writeText(url).then(() => showToast({ text: params ? "Copied app state link" : "Copied app link (no state yet)", ts }),
    () => showToast({ text: "Copy failed", ts }));
}

// A built app linked from message text (a /render?path=… link under the apps root) → {name, dir, params}, else null.
// Extra query keys after the path are the app's state (see copyAppState). Mirror of _app_at in the backend.
// Null until the root is known (OpenBot's "" root would accept any absolute path).
export function appFromText(text: string | null | undefined, rootDir: string = appsRoot()): (AppRef & { params: string }) | null {
  if (!rootDir) return null;
  const root = rootDir.replace(/\/$/, "") + "/";
  for (const m of String(text || "").matchAll(/render\?path=([^\s)\]>"']+)/g)) {
    // A link ending a sentence ("…/brex-memo-updater. You'll hear…") must not keep the period: the card would
    // open `<dir>./index.html`, "no such file".
    const [rawPath, ...rest] = m[1].replace(/[.,;:!?]+$/, "").split("&");
    let p: string;
    try { p = decodeURIComponent(rawPath.replace(/\+/g, " ")); } catch { continue; }
    p = p.replace(/\/index\.html$/, "").replace(/\/$/, "");
    if (!p.startsWith(root)) continue;
    const folder = p.slice(root.length).split("/")[0]; if (!folder) continue;
    const q = new URLSearchParams(rest.join("&")); for (const k of [...q.keys()]) if (k.startsWith("_") || k === "path") q.delete(k);
    return { name: folder.replace(/[-_]+/g, " ").replace(/^./, (c) => c.toUpperCase()), dir: root + folder, params: q.toString() };
  }
  return null;
}

export const agoShort = (t: number): string => {
  const s = Math.max(0, Date.now() / 1000 - t);
  return s < 60 ? "just now" : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 86400 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`;
};

// ------------------------------------------------------------------ the viewer (/bots/apps/<folder>) ----
// Full-size embed, a route of its own (routes.ts): AppViewer mounts on it and owns the iframe. Unlike the card
// thumbnails that iframe is unsandboxed and focusable, so the app's own buttons, params and runPython work. Leaving
// the route unmounts it, so the app stops running.
let viewerOpen = false;
/** AppViewer's mount/unmount: the viewer counts as a holder of the host URL's app params while it shows. */
export function setViewerOpen(on: boolean): void { viewerOpen = on; }
/** The route segment for an app folder: its basename under the apps root. */
export const folderOf = (a: AppLike): string => a.folder || a.dir.replace(/\/+$/, "").split("/").pop() || "";
const routeNow = (): BotsRoute => (botsMounted() ? parseBotsPath(location.pathname) : { kind: "list" });
const onAppsPage = (): boolean => routeNow().kind === "apps";
const viewing = (): boolean => { const r = routeNow(); return r.kind === "apps" && !!r.folder; };

/** The query a navigation inside the Apps page carries: the host keys that are there, plus `params` as the app's
 *  state (REPLACING the previous app's keys, as applyAppParams does) — or no app keys at all when `params` is null. */
function appsQuery(params: string | null): string {
  const q = new URLSearchParams();
  try { for (const [k, v] of new URLSearchParams(location.search)) if (!appParamOk(k)) q.set(k, v); } catch { /* unreadable */ }
  if (params !== null) for (const [k, v] of new URLSearchParams(params)) if (appParamOk(k)) q.set(k, v);
  const s = q.toString();
  return s ? "?" + s : "";
}

/** Open `a` full-size: its params go onto the URL with the navigation, so the viewer's frame reads them at mount. */
export function viewApp(a: AppLike): void {
  navigateUrl(appsHref(folderOf(a)) + appsQuery(a.params || ""));
}
/** Back to the gallery. The closed app's state must not linger on the URL for the next one to read — unless the
 *  side app or an expanded inline card still shows one, in which case the params are ITS live state. */
export function closeView(): void {
  if (!viewing()) return;
  navigateUrl(appsHref() + appsQuery(holders.size ? appStateParams() : null));
}

// ------------------------------------------------------------------ the gallery (#apanel) ----
/** The footer #apps chip: the gallery route. AppsPanel lists the folder on mount. */
export function openApps(): void { navigateUrl(appsHref()); }
/** Back / Esc from the gallery or the viewer: the selected bot's chat; the gallery unmounts, so its iframes go and
 *  idle apps stop. A viewed app's params leave the URL unless another surface still holds them. */
export function closeApps(): void {
  if (!onAppsPage()) return;
  if (viewing() && !holders.size) { clearAppParams(); }
  goChat();
}
