// Data hooks for the widgets whose content is fetched. The apps and sessions
// effects are Home's former strip effects, moved here unchanged apart from
// `rows`: a 2x2 widget draws two rows, so it asks for twice the cards a row fits.
import { useCallback, useEffect, useRef, useState } from "react";
import {
  getAppsPage,
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
import type { AppsSort } from "./layout";
import { MAX_ROW } from "./strip";

export const APPS_PAGE = 48;

/** True once every catalog app is loaded: the running count reaches the
    server's total, or a page came back empty. */
export function pageDone(loaded: number, received: number, total: number): boolean {
  return received === 0 || loaded + received >= total;
}

/** The app catalog (recency-then-name order) for the icons strip, paged
    lazily. Nothing is fetched on mount; the strip calls `loadMore` when it is
    scrolled near its end (or does not yet overflow), and each call appends the
    next APPS_PAGE apps. Calls while a request is pending, while disabled, or
    once `done` are no-ops. A failed page leaves state unchanged. An
    apps-changed announcement resets to empty so the strip asks again. */
export function usePagedApps(enabled: boolean) {
  const [all, setAll] = useState<AppInfo[]>([]);
  const [done, setDone] = useState(false);
  const loaded = useRef<AppInfo[]>([]);
  const doneRef = useRef(false);
  const inFlight = useRef(false);
  const gen = useRef(0);
  useCurrentAppsChanged(() => {
    gen.current += 1;
    inFlight.current = false;
    loaded.current = [];
    doneRef.current = false;
    setAll([]);
    setDone(false);
  });
  const loadMore = useCallback(() => {
    if (!enabled || doneRef.current || inFlight.current) return;
    inFlight.current = true;
    const g = gen.current;
    getAppsPage({ offset: loaded.current.length, limit: APPS_PAGE }).then(
      (r) => {
        if (g !== gen.current) return;
        inFlight.current = false;
        const fin = pageDone(loaded.current.length, r.apps.length, r.total);
        loaded.current = [...loaded.current, ...r.apps];
        doneRef.current = fin;
        setAll(loaded.current);
        setDone(fin);
      },
      () => {
        if (g === gen.current) inFlight.current = false;
      },
    );
  }, [enabled]);
  return { all, loadMore, done };
}

/** Fused apps — hydrate the recent row first. The server only scans the full
    workspace when valid recents do not fill it, preserving discovery and the
    showcase fallback without charging returning visits for an exhaustive walk.
    Both row fetches ask for `limit * rows + 1`: one extra item so the cards
    strip has a next card to peek in at its right edge. Never a fixed MAX_ROW —
    that reintroduces the exhaustive workspace walk documented in strip.ts. */
export function useHomeApps(limit: number | null, rows: number, sort: AppsSort = "opened") {
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
    getHomeApps(Math.min(limit * rows + 1, MAX_ROW), sort).then(
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
          const again = await getHomeApps(Math.min(limit * rows + 1, MAX_ROW), sort);
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
  }, [limit, rows, sort, appsNonce, retry]);
  return { apps, appsError, retry: () => (setApps(null), setAppsError(null), setRetry((n) => n + 1)) };
}

/** Claude session folders — Home's endpoint orders transcript mtimes first,
    then opens only enough newest JSONL files to fill the row. */
export function useHomeSessions(limit: number | null, rows: number) {
  const [sessions, setSessions] = useState<ClaudeSessionFolder[] | null>(null);
  useEffect(() => {
    if (limit === null) return;
    let alive = true;
    getHomeClaudeSessionFolders(Math.min(limit * rows + 1, MAX_ROW)).then(
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
