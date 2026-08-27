// Shared re-render signals. The shell has two distinct "URL changed" tiers
// (mirrors the vanilla shell's route()-vs-syncUpdateButton split):
//
//  - nav epoch:  popstate or an explicit navigate()/navigateUrl(). Route is
//    re-derived and the active view remounts (vanilla rebuilt the view DOM on
//    every route() call — a remount is the faithful equivalent).
//  - url version: ANY history write, including replaceState param writes from
//    iframe runtimes and the layout modes' `_layout` sync. Chrome (bookmark
//    buttons, active-bookmark highlight) re-renders; views do NOT remount.
//
// main.tsx wraps history.replaceState/pushState to dispatch "fused:urlchange"
// (the injected runtime writes params through the parent's history object,
// which fires no native event) — that wrapping is load-bearing for the
// layout modes and the update-bookmark flow, not just for these hooks.
import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { NAV_EVENT } from "@platform/lib/router";
import { createCloseDeferrer } from "@platform/lib/exit-animation";
import { navReach, subscribeNavReach, type NavReach } from "@platform/lib/nav-history";
import {
  getSidebarState,
  subscribeSidebarState,
  type SidebarState,
} from "@platform/lib/sidebarstate";

export function useEventCounter(events: readonly string[]): number {
  const [n, setN] = useState(0);
  useEffect(() => {
    const bump = () => setN((v) => v + 1);
    for (const ev of events) window.addEventListener(ev, bump);
    return () => {
      for (const ev of events) window.removeEventListener(ev, bump);
    };
    // events is a constant array per call site
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return n;
}

export function useNavEpoch(): number {
  return useEventCounter(["popstate", NAV_EVENT]);
}

export function useUrlVersion(): number {
  return useEventCounter(["popstate", NAV_EVENT, "fused:urlchange"]);
}

// Bookmark store change signal. The localStorage store (lib/bookmarks.ts)
// stays a pure data layer; every UI mutation calls notifyBookmarksChanged()
// so all subscribed components (sidebar, breadcrumb star) re-read it.
const BOOKMARKS_EVENT = "fused:bookmarks";

export function notifyBookmarksChanged(): void {
  window.dispatchEvent(new Event(BOOKMARKS_EVENT));
}

export function useBookmarksVersion(): number {
  return useEventCounter([BOOKMARKS_EVENT]);
}

// Armed-bookmark change signal — same store-owned pattern as recents below:
// armBookmark()/disarmBookmark() (lib/bookmarks.ts) dispatch it themselves,
// because not every disarm site coincides with a url or bookmark-store event
// (the Breadcrumb's pathname-change disarm runs in an effect AFTER the sidebar
// has already rendered against the stale armed value).
const ARMED_EVENT = "fused:armchange";

export function notifyArmedChanged(): void {
  window.dispatchEvent(new Event(ARMED_EVENT));
}

export function useArmedVersion(): number {
  return useEventCounter([ARMED_EVENT]);
}

// Run `cb` when the tab regains focus or becomes visible again — the app's
// "re-read cheap state on return" freshness posture (deploy dot, deploy
// pref, account status). One shared subscription instead of per-site
// listener boilerplate, and coalesced: a single tab return fires BOTH
// `focus` and `visibilitychange`, which would double every refresh — calls
// landing in the same tick collapse to one. The callback is kept fresh via
// a ref, so passing an inline closure is fine. Does NOT fire on mount —
// callers own their initial read.
export function useRefreshOnReturn(cb: () => void): void {
  const ref = useRef(cb);
  ref.current = cb;
  useEffect(() => {
    let queued = false;
    const refresh = () => {
      if (queued || document.visibilityState !== "visible") return;
      queued = true;
      window.setTimeout(() => {
        queued = false;
        ref.current();
      }, 0);
    };
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, []);
}

// Exit animation for an overlay whose CALLER owns the unmount (every dialog is
// `{open && <Modal …/>}`, so the overlay can't hold itself on screen — see
// lib/exit-animation). Returns `closing` — true while the exit runs, i.e. the
// frame budget the `.closing` CSS has to play in — and `requestClose`, which
// every close path (Esc, backdrop, ✕) calls INSTEAD of onClose.
//
// The deferrer is created once and reads `onClose` through a ref, so an inline
// arrow closure as onClose (what every call site passes) doesn't tear down and
// rebuild a pending exit mid-animation.
export function useDeferredClose(
  onClose: () => void,
  durationMs: number,
): { closing: boolean; requestClose: () => void } {
  const [closing, setClosing] = useState(false);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const deferrer = useMemo(
    () => createCloseDeferrer(durationMs, () => closeRef.current(), setClosing),
    [durationMs],
  );
  // Drop a pending close on unmount: the caller may have unmounted the overlay
  // for its own reasons (a navigation) and the timer must not fire into it.
  useEffect(() => () => deferrer.cancel(), [deferrer]);
  return { closing, requestClose: deferrer.request };
}

// Tab title reflects whatever's on screen (a file/dir name, or a static
// label like "Panel"), falling back to the bare app name at the root.
// `undefined` means "not this view's title to set" (e.g. App skips it for
// routes StatView owns) so effect ordering can't clobber a sibling's title.
export function useDocumentTitle(label: string | null | undefined): void {
  useEffect(() => {
    if (label === undefined) return;
    document.title = label ? `${label} – Fused Render` : "Fused Render";
  }, [label]);
}

// The shell's own tab icon: READ off the `<link rel="icon">` the document
// arrived with, not spelled here. frontend/index.html says `/favicon.ico`,
// but Vite rewrites that to the build's base (`/static/shell-dist/favicon.ico`,
// vite.config.js) — a hard-coded `/favicon.ico` restore was a 404, which is
// what the blank placeholder on the tab was (owner, 2026-08-27, second
// report). Captured lazily on the first swap, so it is whatever the served
// index.html linked, dev or packaged.
const DEFAULT_FAVICON = Symbol("default favicon");
let defaultHref: string | null = null;

// The fused-render mark's own two colours (frontend/public/favicon.ico: the
// #1b1d21 rounded square, the #e5ff44 glyph). An app's favicon is composed in
// the same livery so every fused tab reads as one family.
const FAVICON_BG = "#1b1d21";
const FAVICON_FG = "#e5ff44";

// Set the tab icon by REPLACING the `<link rel="icon">` node, never by editing
// its href: browsers (Chrome at least) do not reliably refetch when an existing
// link's href flips back to a URL it showed before, which left the previous
// app's icon stuck on a tab until a hard reload (owner, 2026-08-27). A fresh
// node is a fresh icon request every time. The default goes back with a unique
// query string as well — Chrome's per-document favicon cache can answer a URL
// it already holds without repainting; static serving ignores the query, so
// the bytes are the same file under a new key.
let restoreSeq = 0;
function setFaviconHref(href: string | typeof DEFAULT_FAVICON): void {
  const old = document.querySelector<HTMLLinkElement>('link[rel="icon"]');
  if (defaultHref === null && old) defaultHref = old.href;
  const link = document.createElement("link");
  link.rel = "icon";
  if (href === DEFAULT_FAVICON) {
    if (!defaultHref) return;
    const sep = defaultHref.includes("?") ? "&" : "?";
    link.href = `${defaultHref}${sep}r=${++restoreSeq}`;
    link.setAttribute("sizes", "any");
  } else {
    link.href = href;
  }
  if (old) old.replaceWith(link);
  else document.head.appendChild(link);
}

// An app's icon.svg as a favicon in the fused livery: the svg's alpha as a
// yellow glyph, inset on a black rounded square — mirroring the shell's own
// mark (owner, 2026-08-27). Composed on a canvas: the svg is drawn to an
// offscreen layer and recoloured with `source-in` (its shape, our colour — the
// same alpha-mask idea the sidebar uses via CSS mask), then laid over the
// square. Resolves to a PNG data URL, or null when the svg fails to load or
// the canvas is unavailable — callers then leave the default icon in place.
async function composeFavicon(src: string): Promise<string | null> {
  const img = new Image();
  const loaded = new Promise<boolean>((resolve) => {
    img.onload = () => resolve(true);
    img.onerror = () => resolve(false);
  });
  img.src = src;
  if (!(await loaded)) return null;
  const size = 64;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  // The square: radius and inset in the ico's own proportions (a soft corner
  // and a glyph that fills the middle ~56%).
  const r = size * 0.22;
  ctx.fillStyle = FAVICON_BG;
  ctx.beginPath();
  ctx.roundRect(0, 0, size, size, r);
  ctx.fill();
  // The glyph, recoloured on its own layer so the fill cannot bleed onto the
  // square. Drawn with its aspect kept inside the inset box.
  const layer = document.createElement("canvas");
  layer.width = layer.height = size;
  const lc = layer.getContext("2d");
  if (!lc) return null;
  const inset = size * 0.22;
  const box = size - inset * 2;
  const iw = img.naturalWidth || box;
  const ih = img.naturalHeight || box;
  const scale = Math.min(box / iw, box / ih);
  const dw = iw * scale;
  const dh = ih * scale;
  lc.drawImage(img, (size - dw) / 2, (size - dh) / 2, dw, dh);
  lc.globalCompositeOperation = "source-in";
  lc.fillStyle = FAVICON_FG;
  lc.fillRect(0, 0, size, size);
  ctx.drawImage(layer, 0, 0);
  try {
    return canvas.toDataURL("image/png");
  } catch {
    return null;
  }
}

// Tab icon: while a route inside an app is on screen, its optional icon.svg
// (composed in the fused livery, above) replaces the shell's own; the default
// comes back when the route leaves (cleanup) or the href goes null (no icon
// for this app). One writer at a time by construction — the callers (AppPage,
// StatView) are mutually exclusive mounts — so there is no arbitration, only
// the restore. Composition is async, so a `live` flag stops a slow icon from
// landing after the route moved on.
export function useFavicon(href: string | null): void {
  useEffect(() => {
    if (!href) return;
    let live = true;
    composeFavicon(href).then((dataUrl) => {
      if (live && dataUrl) setFaviconHref(dataUrl);
    });
    return () => {
      live = false;
      setFaviconHref(DEFAULT_FAVICON);
    };
  }, [href]);
}

// The builtin-mount readiness hook lived here: a bounded /api/config poll that
// answered "is the bundled zip mount attached and browsable yet", seeded from
// the boot-time config snapshot because the one-shot fetch (main.tsx) lands
// well before the server's background automount thread finishes attaching. It
// gated the sidebar's App Basics entry so it was never a dead link. Both
// consumers are gone — the Claude Config app stopped being a mount when it
// became native React over its own server bridge, the Sessions inbox page was
// deleted on 2026-08-18 (Tasks supersedes it), and the learn content left the
// app for the community catalog. `sessions_mount_ready` still ships on
// /api/config for the next surface that links into a bundled mount; git
// history has the poll if one needs it back.


// Live sidebar chrome state (platform/lib/sidebarstate) — collapsed flag and
// dragged width, shared so every owner of the frame agrees on the layout.
export function useSidebarState(): SidebarState {
  return useSyncExternalStore(subscribeSidebarState, getSidebarState, getSidebarState);
}

// Whether Back / Forward have anywhere to go (platform/lib/nav-history). Not
// `useNavEpoch` + a read: the answer also changes on `currententrychange`, an
// event that fires on `window.navigation` rather than on `window`, so the
// counter hook above cannot see it.
export function useNavReach(): NavReach {
  return useSyncExternalStore(subscribeNavReach, navReach, navReach);
}
