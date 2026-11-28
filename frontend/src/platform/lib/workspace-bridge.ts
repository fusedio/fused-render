// The WORKSPACE's seam (D1514, SPEC §56): how a page — in the host window or in
// one of its frames — reaches the host that owns the tabs. Platform layer and
// DOM-light so `apps/*` can use it; the host's own code is shell/workspace.
//
// A window is a host or not for its whole life: the flag comes from
// /api/config, which main.tsx awaits before the first render, and the host is
// marked there — before any frame exists — so a frame's module-init reads
// (router.ts WS_ROLE's climbs, the runtime's params climb) always see it.
import { useEffect, useRef } from "react";
import { IS_EMBED, WS_ROLE, installNavSink, type WsRole } from "./router";

/** WHAT A PAGE IN A TAB TELLS THE HOST'S RAIL (SPEC §56 WS-20): the rail it
 *  used to draw, as PLAIN DATA — no element and no function crosses a frame;
 *  the host rebuilds icons and turns actions into its own calls. */
export interface WsPublishedPlace {
  /** The rail's place (shell/claude-rail `RailPlace`), as data. */
  place: { kind: string } & Record<string, unknown>;
  /** What the rail's + would make a task on (`NewTaskSeed`). */
  newTask: { target: string | null; waiting: string | null; on: string | null; where?: { canvas: string; node: string | null } };
  /** The Git door's path, or null for none. */
  git: { path: string } | null;
  /** A file's other content modes: each door's name and icon url, and the
   *  frame address of the file drawn in that mode. */
  fileModes: {
    path: string;
    rev: string;
    doors: { mode: string; title: string; icon: string | null }[];
    srcs: Record<string, string>;
  } | null;
  /** A canvas page: the card's node chip and place tags select inside it
   *  (the host sends `WS_SELECT_NODE_EVENT` to the frame). */
  canvasSelect?: boolean;
}

/** The event the host dispatches on a canvas tab's window to select a node
 *  there (detail `{ udfName, sessionId?, tag }`). */
export const WS_SELECT_NODE_EVENT = "fused:ws-select-node";

export interface WorkspaceHostApi {
  /** Open a same-origin page by the open rules (shell/workspace
   *  workspace-lib `planOpen`); a foreign origin opens outside the app. */
  openPage(url: string, opts?: { newTab?: boolean }): void;
  /** Open `url` where it belongs (workspace-lib `classifyNav`): the right
   *  panel for a chat door, Tasks or Bots, else a page by the open rules. */
  openHref?(url: string, opts?: { newTab?: boolean }): void;
  /** Show `view` (an encoded `_right`, null = closed) in the right panel. */
  openRight?(view: string | null, opts?: { replace?: boolean }): void;
  /** A hop a FRAME is about to make: true when the host took it (the right
   *  panel's, or — from the panel — a page for the left); false to let the
   *  frame push it itself. */
  takeHop?(url: string, role: WsRole): boolean;
  /** A tab frame's arrival URL carrying a companion pane (`_side=claude`,
   *  `_mode=git`, …): the host opens it on the right when `win` is the shown
   *  tab, and answers the URL the tab should keep (or null: keep it as is). */
  adoptArrival?(win: Window, url: string): string | null;
  /** The rail a page in a tab would draw (null when it goes). */
  publishPlace?(win: Window, info: WsPublishedPlace | null): void;
  /** The bare `/` in a tab: the panel's chat composer takes the keyboard,
   *  when a chat shows there. True when it did. */
  focusPanelChat?(): boolean;
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
    openHref: (url, opts) => (impl?.openHref ? impl.openHref(url, opts) : fallback(url)),
    openRight: (view, opts) => impl?.openRight?.(view, opts),
    takeHop: (url, role) => impl?.takeHop?.(url, role) ?? false,
    adoptArrival: (win, url) => impl?.adoptArrival?.(win, url) ?? null,
    publishPlace: (win, info) => impl?.publishPlace?.(win, info),
    focusPanelChat: () => impl?.focusPanelChat?.() ?? false,
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
  const role = WS_ROLE;
  installNavSink((href) => {
    const host = workspaceHost();
    if (!host) return false;
    const path = pathOf(href);
    if (path === onboardingPath) {
      host.openPage(href);
      return true;
    }
    // THE RIGHT SIDE'S URLS ARE THE HOST'S (§56 WS-15): a chat door, Tasks or
    // Bots pressed in a page opens in the panel, never in this tab — and from
    // the panel, a page opens on the left. The host decides; false = ours.
    if (host.takeHop?.(href, role)) return true;
    if (WS_ROLE === "home" && path !== "/home" && path !== "/") {
      host.openPage(href, { newTab: true });
      return true;
    }
    return false;
  });
}

/** A TAB FRAME BOOTING ON A COMPANION PANE (§56 WS-25): an old link or
 *  bookmark (`?_side=claude&session_id=…`, `?_mode=git`) names a pane this
 *  page no longer draws in a workspace. The host shows it on the right (when
 *  this tab is the shown one) and the tab keeps its URL without it. Called
 *  once by main.tsx, before the first render. */
export function adoptWorkspaceArrival(): void {
  if (WS_ROLE !== "tab") return;
  const host = workspaceHost();
  if (!host?.adoptArrival) return;
  try {
    const here = location.pathname + location.search;
    const keep = host.adoptArrival(window, here);
    if (keep !== null && keep !== here) history.replaceState(history.state, "", keep + location.hash);
  } catch {
    /* the host went away under us: the page shows what its URL says */
  }
}

/** A page in a tab frame: say what its rail would show (null on unmount).
 *  No-op anywhere else. */
export function publishWorkspacePlace(info: WsPublishedPlace | null): void {
  if (WS_ROLE !== "tab" && WS_ROLE !== "home") return;
  try {
    workspaceHost()?.publishPlace?.(window, info);
  } catch {
    /* host gone */
  }
}

/** A page's rail, said to the host while it is mounted in a tab (WS-20):
 *  again whenever what it says changes (compared as data), withdrawn on
 *  unmount. One document can mount two publishers (the app page's Files tab
 *  holds a folder listing): the higher `rank` speaks, the later on a tie.
 *  Nothing anywhere else. */
const publishers: { token: object; rank: number; info: WsPublishedPlace | null }[] = [];
function flushPublishers(): void {
  let top: (typeof publishers)[number] | null = null;
  for (const p of publishers) if (p.info && (!top || p.rank >= top.rank)) top = p;
  publishWorkspacePlace(top?.info ?? null);
}
export function useWorkspacePlace(info: WsPublishedPlace | null, rank = 0): void {
  const inTab = WS_ROLE === "tab" || WS_ROLE === "home";
  const json = inTab ? JSON.stringify(info) : "";
  const token = useRef({}).current;
  useEffect(() => {
    if (!json) return;
    let entry = publishers.find((p) => p.token === token);
    if (!entry) {
      entry = { token, rank, info: null };
      publishers.push(entry);
    }
    entry.info = JSON.parse(json) as WsPublishedPlace | null;
    flushPublishers();
  }, [json, token, rank]);
  useEffect(() => {
    if (!inTab) return;
    return () => {
      const i = publishers.findIndex((p) => p.token === token);
      if (i !== -1) publishers.splice(i, 1);
      flushPublishers();
    };
  }, [inTab, token]);
}

/** From a page in a tab: open `href` where the host puts it (a chat door or
 *  `_side=git` lands in the right panel). False outside a workspace. */
export function openInWorkspace(href: string): boolean {
  if (WS_ROLE === null) return false;
  const host = workspaceHost();
  if (!host?.openHref) return false;
  host.openHref(href);
  return true;
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
