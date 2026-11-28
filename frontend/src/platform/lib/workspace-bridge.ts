// The WORKSPACE's seam (D1514, SPEC §56): how a page — in the host window or in
// one of its frames — reaches the host that owns the tabs. Platform layer and
// DOM-light so `apps/*` can use it; the host's own code is shell/workspace.
//
// A window is a host or not for its whole life: the flag comes from
// /api/config, which main.tsx awaits before the first render, and the host is
// marked there — before any frame exists — so a frame's module-init reads
// (router.ts WS_ROLE's climbs, the runtime's params climb) always see it.
import { IS_EMBED, WS_ROLE, installNavSink } from "./router";

export interface WorkspaceHostApi {
  /** Open a same-origin page by the open rules (shell/workspace
   *  workspace-lib `planOpen`); a foreign origin opens outside the app. */
  openPage(url: string, opts?: { newTab?: boolean }): void;
  /** The right panel (specified by later clauses). */
  openRight?(view: string): void;
  /** `from` just saved the bookmarks: every other document of this window
   *  (the host's sidebar, the other tabs' ★) re-reads them now. */
  bookmarksWritten?(from: Window): void;
}

/** What the mounted host registers: the API, plus what only the host's own
 *  chrome asks of it. */
export interface WorkspaceHostImpl extends WorkspaceHostApi {
  /** The ACTIVE tab's own URL (path+query) — the host's location minus `_tabs`
   *  and `_right`, and the right answer when the host sits on Home for a
   *  `/render` tab. */
  pageUrl(): string;
}

let hostOn = false;
let impl: WorkspaceHostImpl | null = null;

/** Should this window be a workspace host? Top level, not an embed, flag on. */
export function shouldHostWorkspace(config: { workspace_tabs_enabled?: boolean | null }): boolean {
  if (config.workspace_tabs_enabled !== true || IS_EMBED) return false;
  try {
    return typeof window !== "undefined" && window === window.top;
  } catch {
    return false;
  }
}

/** Mark this window as the host (main.tsx, once, before the first render). The
 *  global is a stable facade: the live implementation is registered by the
 *  host component while it is mounted (`registerWorkspaceImpl`), and while it
 *  is not (the setup wizard owns the window) a request is an ordinary hop. */
export function bootWorkspaceHost(fallback: (url: string) => void): void {
  hostOn = true;
  // Pages inside the frames keep their params to themselves: the host's URL
  // mirrors the ACTIVE tab, so it must be neither a place a frame writes its
  // params (boundary) nor one it reads globals from (opaque, runtime.js).
  window._fusedParamBoundary = true;
  window._fusedParamOpaque = true;
  window._fusedWorkspace = {
    openPage: (url, opts) => (impl ? impl.openPage(url, opts) : fallback(url)),
    openRight: (view) => impl?.openRight?.(view),
    bookmarksWritten: (from) => impl?.bookmarksWritten?.(from),
  };
}

/** Is THIS window a workspace host? */
export function isWorkspaceHost(): boolean {
  return hostOn;
}

export function registerWorkspaceImpl(api: WorkspaceHostImpl | null): void {
  impl = api;
}

/** "The page this window shows", for chrome that compares against it (a
 *  bookmark's active check): the active tab's own URL in a host, the
 *  location everywhere else. */
export function currentPageUrl(): string {
  if (hostOn && impl) return impl.pageUrl();
  return location.pathname + location.search;
}

/** The host that owns this window: its own facade in the host, the parent's in
 *  one of its frames, else null (a nested frame inside a tab asks its own
 *  parent, which is no host — only the host's direct frames talk to it). */
export function workspaceHost(): WorkspaceHostApi | null {
  if (typeof window === "undefined") return null;
  try {
    if (hostOn) return window._fusedWorkspace ?? null;
    if (WS_ROLE === null || window.parent === window) return null;
    return window.parent._fusedWorkspace ?? null;
  } catch {
    return null;
  }
}

function pathOf(url: string): string {
  const cut = url.search(/[?#]/);
  return cut === -1 ? url : url.slice(0, cut);
}

/** In a FRAME: the hops this document must not make itself.
 *   - Home stays Home (§56 WS-7): Home's frame never leaves `/home` — a hop to
 *     any other page opens it as a tab. A query-only change stays here.
 *   - The setup wizard is the whole window, never a tab: the host decides.
 *  Everything else falls through to the ordinary local push. */
export function installWorkspaceFrameSink(onboardingPath: string): void {
  if (WS_ROLE === null) return;
  installNavSink((href) => {
    const host = workspaceHost();
    if (!host) return false;
    const path = pathOf(href);
    if (path === onboardingPath) {
      host.openPage(href);
      return true;
    }
    if (WS_ROLE === "home" && path !== "/home" && path !== "/") {
      host.openPage(href, { newTab: true });
      return true;
    }
    return false;
  });
}

// ---- bookmarks, across the window's documents -------------------------------
//
// Each document keeps its own copy of the bookmarks (platform/lib/bookmarks:
// an in-memory mirror, re-read every 30 s). In a workspace the sidebar and
// every tab are separate documents of ONE window: a ★ pressed in a tab left
// the sidebar's Favourites stale until that poll, and a save made from a stale
// copy could drop the other's change. So a save is announced, and every other
// document re-reads at once.

/** The event a document hears when another one of this window saved. */
export const WS_BOOKMARKS_EVENT = "fused:ws-bookmarks";

/** This document just saved the bookmarks (bookmarks.ts `commit`). Nothing
 *  outside a workspace. */
export function announceBookmarksWritten(): void {
  if (typeof window === "undefined") return;
  try {
    workspaceHost()?.bookmarksWritten?.(window);
  } catch {
    /* a host that went away mid-call */
  }
}

/** Run `refresh` when another document of this window saved the bookmarks. */
export function onWorkspaceBookmarks(refresh: () => void): () => void {
  if (typeof window === "undefined") return () => {};
  window.addEventListener(WS_BOOKMARKS_EVENT, refresh);
  return () => window.removeEventListener(WS_BOOKMARKS_EVENT, refresh);
}
