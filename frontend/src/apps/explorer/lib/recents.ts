// Recents store + tracking hook, persisted server-side at
// ~/.fused-render/recents.json via /api/recents (fused_render/shell/recents.py).
//
// Reads are synchronous off an in-memory cache (same posture as bookmarks.ts);
// the server owns all list logic — dedupe by fs path, newest-first order, the
// 20-entry cap, missing-file filtering — so every mutation here is a POST/PUT
// followed by a cache refresh from the response of a fresh GET. Recording is
// fire-and-forget: a recents failure must never affect the view being opened.
import { useEffect, useRef } from "react";

import { getRecents, postRecentOpen, putRecentsCollapsed } from "@platform/lib/api";
import type { RecentEntry, RecentsResult } from "@platform/lib/api";
import { useEventCounter } from "@platform/lib/hooks";
import { stripSessionParams } from "@platform/lib/session-params";
import { IS_EMBED, VIEW_PREFIX, currentUrl, fsPathFromLocation, rootedFsPath } from "@platform/lib/router";

export type { RecentEntry };

// Store change signal — explorer-owned (recents are an explorer concept, not a
// shell/platform one; the app-builder keeps its own independent recents in
// apps/builder/lib/recents.ts). The store dispatches it itself after every
// cache advance.
const RECENTS_EVENT = "fused:recents";

export function notifyRecentsChanged(): void {
  window.dispatchEvent(new Event(RECENTS_EVENT));
}

export function useRecentsVersion(): number {
  return useEventCounter([RECENTS_EVENT]);
}

let cache: RecentsResult = { collapsed: false, entries: [] };

export function loadRecents(): RecentsResult {
  return cache;
}

// The fs path a recent entry targets, decoded from its explorer url — the
// entry's stable identity: the url mutates on every live param write, the
// path doesn't (React row keys and the slot order below key on it). The bare
// legacy "/view/" prefix still decodes (entries recorded before the /explorer
// namespace rename).
export function recentFsPath(url: string): string {
  const qIdx = url.indexOf("?");
  const pathname = qIdx !== -1 ? url.slice(0, qIdx) : url;
  const prefix = [VIEW_PREFIX, "/view/"].find((p) => pathname.startsWith(p));
  if (!prefix) return pathname;
  return rootedFsPath(
    pathname.slice(prefix.length).split("/").filter(Boolean).map(decodeURIComponent).join("/")
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

// Drop the rows for a path the user just DELETED (trashed or hard-deleted), and for
// everything inside it when that path was a folder — the same prefix + "/"
// containment test `clearClipboardIfDeleted` uses.
//
// The GET already hides entries whose file is gone (RC-7), but nothing re-GETs
// after a delete: the row would sit in the sidebar pointing at a file that no
// longer exists until the user's next navigation happened to refresh the cache.
// So the delete tells us, and the cache drops it right there.
//
// Deliberately LOCAL — no request. The server's list runs deeper than the three
// displayed rows (RC-6's 20-entry buffer exists for exactly this), so the vacated
// slot refills from the cache we already hold, with no round-trip and nothing to
// wait for. A re-GET would also be the wrong tool: RC-7's existence check fails
// OPEN on an indeterminate answer (rc down, budget exceeded), which for a row the
// user just deleted would mean watching it come back. Nothing is written to the
// store — the entry stays on disk, hidden, exactly as RC-7 has it, so a file
// restored from the Bin legitimately reappears in Recents.
export function dropRecentsFor(deleted: string): void {
  const kept = cache.entries.filter((e) => {
    const path = recentFsPath(e.url);
    return path !== deleted && !path.startsWith(deleted + "/");
  });
  if (kept.length === cache.entries.length) return;
  cache = { ...cache, entries: kept };
  // Same slot arithmetic a vanished-on-refresh entry gets (RC-11): the freed slot
  // is filled from the bottom by the next MRU entry, survivors do not reshuffle.
  slotPaths = computeSlots(slotPaths, cache.entries);
  notifyRecentsChanged();
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
// each displayed slot's (path, latest url, latest title). Urls are INCLUDED —
// a stale href is a real bug (middle-click/copy-link navigates to outdated
// params, RC-3); with stable slots + path-keyed rows a url-only notify
// re-renders the anchor attributes in place with zero movement and zero
// remounts. Title is included for the same reason: the open-record and the
// title-report re-record that follows it share the same url (only the title
// differs), so a url-only signature would miss that change entirely and the
// row would keep showing the basename after the cache already has the title.
function displaySignature(slots: string[], entries: RecentEntry[], collapsed: boolean): string {
  const byPath = new Map(entries.map((e) => [recentFsPath(e.url), e]));
  return JSON.stringify([
    collapsed,
    slots.map((p) => {
      const e = byPath.get(p);
      return [p, e?.url, e?.title];
    }),
  ]);
}

async function refresh(): Promise<void> {
  const prevSig = displaySignature(slotPaths, cache.entries, cache.collapsed);
  cache = await getRecents();
  // A snapshot fetched while a collapse write is in flight predates that
  // write — the user's newest intent stays authoritative over it.
  if (pendingCollapsed !== null) cache = { ...cache, collapsed: pendingCollapsed };
  slotPaths = computeSlots(slotPaths, cache.entries);
  // Notify only when the visible slice changed; identical-signature refreshes
  // (e.g. a re-record of an unchanged url) stay render-free.
  if (displaySignature(slotPaths, cache.entries, cache.collapsed) !== prevSig) {
    notifyRecentsChanged();
  }
}

// Load the cache once at boot (main.tsx, beside hydrateBookmarks).
export function hydrateRecents(): Promise<void> {
  return enqueue(() =>
    refresh().catch((e) => console.error("[fused] failed to load recents:", e))
  );
}

// SESSION-ONLY PARAMS ARE STRIPPED FIRST (LSN-12, D326). Recents is an AUTOMATIC
// capture that lands on disk: this hook fires on every `fused:urlchange`, so
// without this a click that CLOSES the file preview's companion sidebar
// (`?_side=off`) is recorded, and every later open from the Recents list comes up
// shut — the sidebar's state persisted for good, by a write the user never asked
// for, which is the exact thing D326 exists to stop. The URL a recent replays must
// hold what the file WAS, not what the chrome around it was doing.
//
// Deliberately NOT applied to BOOKMARKS: a bookmark is an explicit "save this
// view" gesture and SB-2 says it captures the URL verbatim, which is how `_mode`,
// sort and a chosen `_side` companion all end up in one. Same param, opposite
// answer, because one capture is chosen and the other is a side effect.
function stripRecordedParams(url: string): string {
  const q = url.indexOf("?");
  if (q < 0) return url;
  const kept = stripSessionParams(url.slice(q + 1));
  return kept === "" ? url.slice(0, q) : url.slice(0, q + 1) + kept;
}

// Record an open (or a live param update) of the current file view. The
// server dedupes by target fs path — a re-record of an already-listed file
// moves it to the top and replaces its url — and no-ops for anything that is
// not an existing file's /view/ url, so the caller stays dumb about the
// target's kind. `title`, when known (the page's own <title>, see App.tsx's
// StatView), is stored alongside the url so the sidebar row can prefer it
// over the file's basename — same posture as bookmark naming.
export function recordRecentOpen(url: string, title?: string | null): Promise<void> {
  return enqueue(async () => {
    try {
      await postRecentOpen(stripRecordedParams(url), title);
      await refresh();
    } catch (e) {
      console.error("[fused] failed to record recent open:", e);
    }
  });
}

// The user's latest collapse intent, authoritative while any write is in
// flight: a refresh() snapshot fetched before the PUT landed must not undo
// the optimistic flip, and under rapid toggles only the NEWEST intent may
// win — an older call never re-asserts its own stale value on completion.
let pendingCollapsed: boolean | null = null;
let collapseWritesInFlight = 0;

export function setRecentsCollapsed(collapsed: boolean): Promise<void> {
  // Optimistic: the flip paints NOW, not a network round-trip later — the
  // collapsed rail's Recents icon expands the sidebar in the same click and
  // must reveal the list it promised, and the heading toggle should not lag
  // the pointer either. Persistence follows behind, serialized by enqueue.
  pendingCollapsed = collapsed;
  collapseWritesInFlight++;
  cache = { ...cache, collapsed };
  notifyRecentsChanged();
  return enqueue(async () => {
    let persisted = true;
    try {
      await putRecentsCollapsed(collapsed);
    } catch (e) {
      persisted = false;
      console.error("[fused] failed to persist recents collapse:", e);
    }
    if (--collapseWritesInFlight > 0) return; // a newer intent is still writing
    pendingCollapsed = null;
    // The cache already shows the latest intent; a successful FINAL write
    // just confirmed the server agrees. A failed final write means the
    // server kept some earlier state — reload it rather than lie about what
    // survives a refresh.
    if (!persisted) {
      await refresh().catch((e) => console.error("[fused] failed to reload recents:", e));
    }
  });
}

// Track-on-open + live param updates. Mounted by StatView: records once when
// the stat confirms a file, then re-records the current url on every param
// write (fused:urlchange — the iframe runtime's replaceState is wrapped in
// main.tsx) with a 500 ms debounce against slider-style param churn. Embed
// panes, directories, and not-yet-stat'd opens (isDir null) opt out; the server
// rejects non-file urls anyway. This confirmed-file gate is the last survivor
// of the seam it used to share with the per-file session restore's tracking
// hook (lib/session.ts, removed in D329).
// `title` is the previewed page's own <title>, when known — it arrives async
// (after the iframe loads), so it is also a dependency: once it resolves, the
// effect re-runs and re-records the current url with the now-known title,
// same as a live param update would.
export function useRecentsTracking(fsPath: string, isDir: boolean | null, title: string | null): void {
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
        void recordRecentOpen(pending, title);
        pending = null;
      }
    };
    // The open itself (session restore's replaceState re-records with the
    // restored params). Guarded against a same-tick navigation race.
    if (fsPathFromLocation() === fsPath) void recordRecentOpen(currentUrl(), title);
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
    // fsPath + isDir identify the open; title is included so its late arrival
    // triggers a re-record.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fsPath, isDir, title]);
}
