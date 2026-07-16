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
import { IS_EMBED, currentUrl, fsPathFromLocation } from "./router";

export type { RecentEntry };

let cache: RecentsResult = { collapsed: false, entries: [] };

export function loadRecents(): RecentsResult {
  return cache;
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

async function refresh(): Promise<void> {
  cache = await getRecents();
  notifyRecentsChanged();
}

// Load the cache once at boot (main.tsx, beside hydrateBookmarks).
export function hydrateRecents(): Promise<void> {
  return enqueue(() =>
    refresh().catch((e) => console.error("[fused] failed to load recents:", e))
  );
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
    const record = () => {
      // The debounced callback can outlive a same-tick navigation race;
      // only record while the shell still shows this file.
      if (fsPathFromLocation() !== fsPath) return;
      void recordRecentOpen(currentUrl());
    };
    record(); // the open itself (session restore's replaceState re-records with the restored params)
    const onUrlChange = () => {
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(record, 500);
    };
    window.addEventListener("fused:urlchange", onUrlChange);
    window.addEventListener("popstate", onUrlChange);
    return () => {
      window.removeEventListener("fused:urlchange", onUrlChange);
      window.removeEventListener("popstate", onUrlChange);
      window.clearTimeout(timer.current);
    };
    // fsPath + isDir identify the open, like useSessionTracking.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fsPath, isDir]);
}
