// Apps (OpenBot src/apps.js, the non-React half): the apps root, the embed/open URLs, the host-params bridge, Copy
// state, appFromText, agoShort. The gallery/viewer live in AppsPanel.tsx/AppViewer.tsx, the thread card in
// AppCard.tsx, the side app in side.ts/SideApp.tsx (showAppBeside is re-exported here for the thread).
//
// URLs: `/render?path=<dir>/index.html` (fused-render's app frame; FusedBot's was `/embed?path=`), so
// every extra key is appended with `&`. A gallery thumbnail carries `_preview=1`, which makes the framed runtime its
// own params target (runtime.js findTarget), so twelve thumbnails never write onto this page's URL; the viewer, the
// side app and the inline card carry no `_preview`, so their params land on THIS page's URL exactly as in OpenBot.
import { useSyncExternalStore } from "react";
import type { AppRef } from "../lib/api";
import { closePanel, getState, openPanel, showToast } from "../state/store";
import { buildsRoot, rootReady, setBuildsRoot, useBuildsRoot } from "../builds/builds";

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
// `new` is this page's own one-shot (?new=1); `peek` is the shell's task side peek (shell/task-peek-store PEEK_PARAM),
// which can sit on /bots too. Neither is an app's state.
export const HOST_KEYS = new Set(["bot", "new", "peek"]);
export const appParamOk = (k: string): boolean => !HOST_KEYS.has(k) && k !== "path" && !k.startsWith("_");

/** The app's live state: this page's query minus host keys, `path` and `_*`. */
export function appStateParams(search: string = typeof location === "undefined" ? "" : location.search): string {
  const q = new URLSearchParams();
  try { for (const [k, v] of new URLSearchParams(search)) if (appParamOk(k)) q.set(k, v); } catch { /* unreadable */ }
  return q.toString();
}

/** Write an app's params onto this page's URL (replaceState). The shell's main.tsx wraps replaceState to dispatch
 *  `fused:urlchange`, which the runtime and the store follow. */
export function applyAppParams(params?: string | null): void {
  try {
    const u = new URL(location.href);
    for (const [k, v] of new URLSearchParams(params || "")) if (appParamOk(k)) u.searchParams.set(k, v);
    if (u.href !== location.href) history.replaceState(history.state, "", u.href);
  } catch { /* no history (tests) */ }
}

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
    const [rawPath, ...rest] = m[1].split("&");
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

// ------------------------------------------------------------------ the viewer (#vpanel) ----
// Full-size embed. Unlike the card thumbnails this iframe is unsandboxed and focusable, so the app's own buttons, params
// and runPython work. Closing blanks the frame so the app stops running. The frame is AppViewer's, driven from here.
let viewed: AppLike | null = null;
const viewSubs = new Set<() => void>();
const viewEmit = () => { for (const l of [...viewSubs]) l(); };
let vframe: HTMLIFrameElement | null = null;
export const getViewedApp = (): AppLike | null => viewed;
export function useViewedApp(): AppLike | null {
  return useSyncExternalStore((l) => { viewSubs.add(l); return () => { viewSubs.delete(l); }; }, getViewedApp, getViewedApp);
}
export function setViewFrame(el: HTMLIFrameElement | null): void { vframe = el; }
export function viewFrameSrc(src: string): void { if (vframe) vframe.src = src; }

export function viewApp(a: AppLike): void {
  viewed = a;
  applyAppParams(a.params);
  viewFrameSrc(appEmbedBase(a.dir, a.params));
  viewEmit();
}
export function closeView(): void {
  if (!viewed) return;
  viewed = null;
  viewFrameSrc("about:blank");
  viewEmit();
}

// ------------------------------------------------------------------ the gallery (#apanel) ----
/** The footer #apps chip. AppsPanel (re)loads the listing whenever the panel opens. */
export function openApps(): void { openPanel("apps"); }
/** Back / Esc / leaving for another panel: the viewer closes and the gallery drops its iframes so idle apps stop. */
export function closeApps(): void {
  closeView();
  if (getState().ui.panel === "apps") closePanel();
}
