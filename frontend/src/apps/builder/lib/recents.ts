// App-builder recents — the builder sidebar's "Recents" section, persisted
// server-side at ~/.fused-render/app_recents.json via /api/apps/recents
// (fused_render/server/routers/apps.py). Fully independent of the explorer's
// file recents (apps/explorer/lib/recents.ts): entries identify an app by
// (tag, name), not by url — the server owns dedupe, order, and cap, so every
// mutation here is a POST followed by a cache refresh. Recording is
// fire-and-forget: a recents failure must never affect opening the app.
import { useEffect } from "react";

import { useEventCounter } from "@platform/lib/hooks";
import { IS_EMBED, appRouteSegments, appUrlForFsPath } from "@platform/lib/router";
import { LINKED_TAG } from "@platform/lib/appEntry";

export interface AppRecentEntry {
  tag: string;
  name: string;
  openedAt: string;
  title?: string | null;
}

const APP_RECENTS_EVENT = "fused:app-recents";

export function notifyAppRecentsChanged(): void {
  window.dispatchEvent(new Event(APP_RECENTS_EVENT));
}

export function useAppRecentsVersion(): number {
  return useEventCounter([APP_RECENTS_EVENT]);
}

let cache: AppRecentEntry[] = [];

export function loadAppRecents(): AppRecentEntry[] {
  return cache;
}

// Serial promise chain like the explorer store's enqueue: record bursts never
// interleave their GET-after-write refreshes, so the cache can't step
// backwards to a stale read.
let tail: Promise<unknown> = Promise.resolve();

function enqueue<T>(op: () => Promise<T>): Promise<T> {
  const run = tail.then(op, op);
  tail = run.catch(() => {});
  return run;
}

async function refresh(): Promise<void> {
  const res = await fetch("/api/apps/recents");
  const data = (await res.json()) as { entries?: AppRecentEntry[] };
  if (!res.ok) throw new Error("failed to load app recents");
  const entries = Array.isArray(data.entries) ? data.entries : [];
  const changed = JSON.stringify(entries) !== JSON.stringify(cache);
  cache = entries;
  if (changed) notifyAppRecentsChanged();
}

export function hydrateAppRecents(): Promise<void> {
  return enqueue(() =>
    refresh().catch((e) => console.error("[fused] failed to load app recents:", e))
  );
}

export function recordAppOpen(tag: string, name: string, title?: string | null): Promise<void> {
  return enqueue(async () => {
    try {
      await fetch("/api/apps/recents/open", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Fused": "1" },
        body: JSON.stringify({ tag, name, title: title ?? undefined }),
      });
      await refresh();
    } catch (e) {
      console.error("[fused] failed to record app open:", e);
    }
  });
}

// Track an app open from the builder route (/apps/<tag>/<name>). Records once
// per mount — the host StatView remounts per navigation, so every open records —
// and re-records when the app's rendered title arrives, so the sidebar row can
// prefer it over the folder name.
export function useAppRecentsTracking(
  fsPath: string,
  fusedDir: string,
  title: string | null
): void {
  useEffect(() => {
    // `fusedDir` empty = tracking disabled (the host view isn't a builder
    // route) — the whole effect is a no-op then.
    if (IS_EMBED || !fusedDir) return;
    // A linked app's route (/apps/linked/<name>) carries its identity
    // directly — its folder lives outside the workspace, so the fs-path
    // derivation below can never reconstruct it. The server resolves the
    // "linked" tag through the registry on both record and read.
    const segs = appRouteSegments(location.pathname);
    if (segs?.tag === LINKED_TAG) {
      void recordAppOpen(segs.tag, segs.name, title);
      return;
    }
    const url = appUrlForFsPath(fsPath, fusedDir);
    if (url === null || location.pathname !== url.split("?")[0]) return;
    const [tag, name] = fsPath
      .slice(fusedDir.replace(/\/+$/, "").length + 1)
      .split("/");
    if (tag && name) void recordAppOpen(tag, name, title);
  }, [fsPath, fusedDir, title]);
}
