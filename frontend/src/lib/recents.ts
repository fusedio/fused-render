// Recents store + tracking hook, persisted server-side at
// ~/.fused-render/recents.json via /api/recents (fused_render/shell/recents.py).
//
// Reads are synchronous off an in-memory cache (same posture as bookmarks.ts);
// the server owns all list logic — dedupe by fs path, newest-first order, the
// 20-entry cap, missing-file filtering — so every mutation here is a POST/PUT
// followed by a cache refresh from the response of a fresh GET. Recording is
// fire-and-forget: a recents failure must never affect the view being opened.
import { useEffect, useRef } from "react";

import { getRecents, postRecentOpen, putRecentsCollapsed } from "./api";
import type { RecentEntry, RecentsResult } from "./api";
import { notifyRecentsChanged } from "./hooks";
import { IS_EMBED, VIEW_PREFIX, currentUrl, fsPathFromLocation, rootedFsPath } from "./router";

export type { RecentEntry };

let cache: RecentsResult = { collapsed: false, entries: [] };

export function loadRecents(): RecentsResult {
  return cache;
}

// The fs path a recent entry targets, decoded from its /view/ url — the
// entry's stable identity: the url mutates on every live param write, the
// path doesn't (React row keys and the slot order below key on it).
export function recentFsPath(url: string): string {
  const qIdx = url.indexOf("?");
  const pathname = qIdx !== -1 ? url.slice(0, qIdx) : url;
  if (!pathname.startsWith(VIEW_PREFIX)) return pathname;
  return rootedFsPath(
    pathname.slice(VIEW_PREFIX.length).split("/").filter(Boolean).map(decodeURIComponent).join("/"),
  );
}

// --- stable-slot display order ----------------------------------------------
//
// The DATA is strict MRU (the server moves a re-recorded file to the top),
// but displaying raw MRU makes the list jump under the user's own pointer:
// clicking a shown recent, or param churn on the open file, would reshuffle
// rows mid-interaction. So the visible top-3 uses session-scoped stable
// slots: a displayed file keeps its slot for the whole page session — its
// row just updates in place — and the only movement is a file NOT currently
// displayed entering at the top (a real navigation), pushing the bottom row
// out. A displayed file that vanishes (deleted; GET filters it) leaves its
// slot and the next MRU entry fills in at the BOTTOM — survivors never
// reshuffle. Not persisted: on boot the slots seed from server MRU order.

const DISPLAY_ROWS = 3;

let slotPaths: string[] = [];

function computeSlots(prev: string[], entries: RecentEntry[]): string[] {
  const mruPaths = entries.map((e) => recentFsPath(e.url));
  const alive = new Set(mruPaths);
  // Vanished files leave their slot; survivors keep their relative order.
  let slots = prev.filter((p) => alive.has(p));
  // A file not currently displayed entering at the MRU head is a real new
  // open -> the one allowed movement: insert at top, bottom row falls out.
  const head = mruPaths[0];
  if (head !== undefined && !slots.includes(head)) slots = [head, ...slots];
  // Fill any remaining vacancies from the bottom, in MRU order.
  for (const p of mruPaths) {
    if (slots.length >= DISPLAY_ROWS) break;
    if (!slots.includes(p)) slots.push(p);
  }
  return slots.slice(0, DISPLAY_ROWS);
}

// The entries to display, in stable-slot order (each slot carries its file's
// LATEST entry — url updates land in place). Idempotent per cache state, so
// safe to call on every sidebar render.
export function displayRecents(): RecentEntry[] {
  slotPaths = computeSlots(slotPaths, cache.entries);
  const byPath = new Map(cache.entries.map((e) => [recentFsPath(e.url), e]));
  return slotPaths.flatMap((p) => byPath.get(p) ?? []);
}

// Serial promise chain like bookmarks.ts's enqueue: recording bursts (open +
// the debounced param updates) and the collapse toggle never interleave their
// GET-after-write refreshes, so the cache can't step backwards to a stale read.
let tail: Promise<unknown> = Promise.resolve();

function enqueue<T>(op: () => Promise<T>): Promise<T> {
  const run = tail.then(op, op);
  tail = run.catch(() => {});
  return run;
}

// What the sidebar actually renders from this store: the collapse flag plus
// each displayed slot's (path, latest url). Urls are INCLUDED — a stale href
// is a real bug (middle-click/copy-link navigates to outdated params, RC-3);
// with stable slots + path-keyed rows a url-only notify re-renders the anchor
// attributes in place with zero movement and zero remounts.
function displaySignature(slots: string[], entries: RecentEntry[], collapsed: boolean): string {
  const byPath = new Map(entries.map((e) => [recentFsPath(e.url), e]));
  return JSON.stringify([collapsed, slots.map((p) => [p, byPath.get(p)?.url])]);
}

async function refresh(): Promise<void> {
  const prevSig = displaySignature(slotPaths, cache.entries, cache.collapsed);
  cache = await getRecents();
  slotPaths = computeSlots(slotPaths, cache.entries);
  // Notify only when the visible slice changed; identical-signature refreshes
  // (e.g. a re-record of an unchanged url) stay render-free.
  if (displaySignature(slotPaths, cache.entries, cache.collapsed) !== prevSig) {
    notifyRecentsChanged();
  }
}

// Load the cache once at boot (main.tsx, beside hydrateBookmarks).
export function hydrateRecents(): Promise<void> {
  return enqueue(() => refresh().catch((e) => console.error("[fused] failed to load recents:", e)));
}

// Record an open (or a live param update) of the current file view. The
// server dedupes by target fs path — a re-record of an already-listed file
// moves it to the top and replaces its url — and no-ops for anything that is
// not an existing file's /view/ url, so the caller stays dumb about the
// target's kind.
export function recordRecentOpen(url: string): Promise<void> {
  return enqueue(async () => {
    try {
      await postRecentOpen(url);
      await refresh();
    } catch (e) {
      console.error("[fused] failed to record recent open:", e);
    }
  });
}

export function setRecentsCollapsed(collapsed: boolean): Promise<void> {
  return enqueue(async () => {
    try {
      await putRecentsCollapsed(collapsed);
      cache = { ...cache, collapsed };
      notifyRecentsChanged();
    } catch (e) {
      console.error("[fused] failed to persist recents collapse:", e);
    }
  });
}

// Track-on-open + live param updates. Mounted by StatView beside
// useSessionTracking (the same seam, lib/session.ts): records once when the
// stat confirms a file, then re-records the current url on every param write
// (fused:urlchange — the iframe runtime's replaceState is wrapped in main.tsx)
// with a 500 ms debounce against slider-style param churn. Embed panes,
// directories, and not-yet-stat'd opens (isDir null) opt out, mirroring
// session tracking; the server rejects non-file urls anyway.
export function useRecentsTracking(fsPath: string, isDir: boolean | null): void {
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (IS_EMBED || isDir !== false) return;
    // The url waiting out the debounce. Captured at EVENT time, while the
    // shell still shows this file — the unmount flush below runs after the
    // location has already moved on, so reading currentUrl() there would
    // either drop the update or record the next view's url.
    let pending: string | null = null;
    const flush = () => {
      window.clearTimeout(timer.current);
      if (pending !== null) {
        void recordRecentOpen(pending);
        pending = null;
      }
    };
    // The open itself (session restore's replaceState re-records with the
    // restored params). Guarded against a same-tick navigation race.
    if (fsPathFromLocation() === fsPath) void recordRecentOpen(currentUrl());
    const onUrlChange = () => {
      if (fsPathFromLocation() !== fsPath) return; // navigated away — not ours
      pending = currentUrl();
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(flush, 500);
    };
    window.addEventListener("fused:urlchange", onUrlChange);
    window.addEventListener("popstate", onUrlChange);
    return () => {
      window.removeEventListener("fused:urlchange", onUrlChange);
      window.removeEventListener("popstate", onUrlChange);
      // Navigating away inside the debounce window must not lose the last
      // param state — flush the captured url instead of dropping it.
      flush();
    };
    // fsPath + isDir identify the open, like useSessionTracking.
  }, [fsPath, isDir]);
}
