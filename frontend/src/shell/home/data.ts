// Data hooks for the widgets whose content is fetched. The apps and sessions
// effects are Home's former strip effects, moved here unchanged apart from
// `rows`: a 2x2 widget draws two rows, so it asks for twice the cards a row fits.
import { useEffect, useState } from "react";
import {
  getHomeApps,
  getHomeClaudeSessionFolders,
  getTasks,
  type AppInfo,
  type ClaudeSessionFolder,
  type Task,
} from "@platform/lib/api";
import { runCommunity } from "@platform/lib/community";
import { useCurrentAppsChanged, TASKS_CHANGED_EVENT } from "@platform/lib/tasksChanged";
import { api as botsApi, type Bot } from "@apps/bots/lib/api";
import { MAX_ROW } from "./strip";

/** Fused apps — hydrate the recent row first. The server only scans the full
    workspace when valid recents do not fill it, preserving discovery and the
    showcase fallback without charging returning visits for an exhaustive walk. */
export function useHomeApps(limit: number | null, rows: number) {
  const [apps, setApps] = useState<AppInfo[] | null>(null);
  const [appsError, setAppsError] = useState<string | null>(null);
  // Bumped on the desk-changed announcement (an icon picked from the sidebar
  // while this row is on screen) so the cards redraw with the new icon.svg.
  // Refetch in place: `apps` is not cleared, so the row never flashes back to
  // skeletons.
  const [appsNonce, setAppsNonce] = useState(0);
  // Bumped by the Retry button of the error state.
  const [retry, setRetry] = useState(0);
  useCurrentAppsChanged(() => setAppsNonce((n) => n + 1));
  useEffect(() => {
    if (limit === null) return;
    let alive = true;
    getHomeApps(Math.min(limit * rows, MAX_ROW)).then(
      async (r) => {
        if (!alive) return;
        if (r.apps.length > 0) {
          setAppsError(null);
          setApps(r.apps.slice(0, MAX_ROW));
          return;
        }
        // Empty on a brand-new install usually isn't "no apps" — it's this
        // fetch landing before the startup showcase clone has finished.
        // Apps.tsx escalates the same "no-cache" catalog status into a
        // wait-for-clone-then-refetch; Home is the first page a new user sees.
        try {
          const local = await runCommunity<{ status?: string }>({ action: "catalog" });
          if (!alive) return;
          // A "no-cache" status means the clone is still missing — wait for
          // it. But the clone can just as easily land in the gap between the
          // first empty getHomeApps and this very check, which reports it
          // "ok" already: that walk never re-ran, so its emptiness is just as
          // stale. Either way, one more walk is needed before the row really
          // is empty — retry unconditionally.
          if (local.status === "no-cache") {
            await runCommunity({ action: "refresh" });
            if (!alive) return;
          }
          const again = await getHomeApps(Math.min(limit * rows, MAX_ROW));
          if (!alive) return;
          setApps(again.apps.slice(0, MAX_ROW));
          return;
        } catch (e) {
          // The clone refused (no usable git, network, a foreign folder at
          // <workspace>/showcase). The reason belongs in the empty state; the
          // backend composes the whole sentence and it renders verbatim.
          if (!alive) return;
          setAppsError((e as Error).message);
        }
        setApps([]);
      },
      (e: Error) => {
        if (!alive) return;
        setApps([]);
        setAppsError(e.message);
      },
    );
    return () => {
      alive = false;
    };
  }, [limit, rows, appsNonce, retry]);
  return { apps, appsError, retry: () => (setApps(null), setAppsError(null), setRetry((n) => n + 1)) };
}

/** Claude session folders — Home's endpoint orders transcript mtimes first,
    then opens only enough newest JSONL files to fill the row. */
export function useHomeSessions(limit: number | null, rows: number) {
  const [sessions, setSessions] = useState<ClaudeSessionFolder[] | null>(null);
  useEffect(() => {
    if (limit === null) return;
    let alive = true;
    getHomeClaudeSessionFolders(Math.min(limit * rows, MAX_ROW)).then(
      (r) => alive && setSessions(r.folders.slice(0, MAX_ROW)),
      () => alive && setSessions([]),
    );
    return () => {
      alive = false;
    };
  }, [limit, rows]);
  return sessions;
}

export interface AsyncState<T> {
  data: T | null;
  error: string | null;
  retry: () => void;
}

/** Open tasks (everything but done/archived) — refetched when anything
    announces a task change, and on a slow beat so a run finishing in the
    background shows up. */
export function useOpenTasks(): AsyncState<Task[]> {
  const [data, setData] = useState<Task[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    const bump = () => setNonce((n) => n + 1);
    window.addEventListener(TASKS_CHANGED_EVENT, bump);
    const t = setInterval(bump, 15000);
    return () => {
      window.removeEventListener(TASKS_CHANGED_EVENT, bump);
      clearInterval(t);
    };
  }, []);
  useEffect(() => {
    let alive = true;
    getTasks().then(
      (r) => {
        if (!alive) return;
        setError(null);
        setData(r.tasks.filter((t) => t.status !== "done" && t.status !== "archived" && t.kind !== "draft"));
      },
      (e: Error) => alive && setError(e.message || "Couldn't load tasks."),
    );
    return () => {
      alive = false;
    };
  }, [nonce]);
  return {
    data,
    error,
    retry: () => {
      setData(null);
      setError(null);
      setNonce((n) => n + 1);
    },
  };
}

/** Bots from the bots status endpoint, polled gently while mounted. */
export function useBots(): AsyncState<Bot[]> {
  const [data, setData] = useState<Bot[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const tick = () => {
      botsApi.status({ cursors: {}, shot_for: "", fast: true }).then(
        (r) => {
          if (!alive) return;
          setError(null);
          setData(r.bots ?? []);
          timer = setTimeout(tick, 10000);
        },
        (e: Error) => {
          if (!alive) return;
          setError(e.message || "Couldn't reach bots.");
        },
      );
    };
    tick();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, [nonce]);
  return {
    data,
    error,
    retry: () => {
      setData(null);
      setError(null);
      setNonce((n) => n + 1);
    },
  };
}
