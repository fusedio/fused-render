// The System chip's poll of `GET /api/system/activity` (shell/SystemDock.tsx),
// one shared snapshot however many components read it.
//
// CADENCE (the live-UI convention aiRuntime.ts set): 1 s while someone is
// looking closely (the System popover open or pinned) or while any process is
// busy (> 5% CPU), 30 s otherwise, and nothing at all while the document is
// hidden. The server samples only while it is being read and idles 15 s after
// the last read — so the slow cadence is deliberately LONGER than that: an
// idle chip lets the 1 Hz sampler stop between reads instead of keeping it
// awake forever. The price is that such a read is a cold start, whose CPU is
// still unknown (it needs two samples), so a read with `totals.cpuPct === null`
// is retried after 1 s, up to `COLD_RETRIES` times, before going slow again.
import { useEffect, useSyncExternalStore } from "react";
import { getSystemActivity, type SystemActivity } from "@platform/lib/sysmon";

export { formatBytes, formatCpu } from "@platform/lib/sysmon";

export const FAST_POLL_MS = 1_000;
export const SLOW_POLL_MS = 30_000;
/** Fast re-reads after a cold read that had no CPU figure yet. */
export const COLD_RETRIES = 2;
/** A process above this CPU % keeps the poll fast even with the popover shut. */
export const BUSY_CPU_PCT = 5;

/** How long to wait before the next poll. Pure, for the tests. `nullReads`
 *  counts the consecutive reads whose CPU total was still unknown. */
export function pollIntervalFor(
  data: SystemActivity | null,
  wantFast: boolean,
  nullReads = 0,
): number {
  if (wantFast) return FAST_POLL_MS;
  if (nullReads > 0 && nullReads <= COLD_RETRIES) return FAST_POLL_MS;
  if (data?.procs.some((p) => (p.cpuPct ?? 0) > BUSY_CPU_PCT)) return FAST_POLL_MS;
  return SLOW_POLL_MS;
}

let snapshot: SystemActivity | null = null;
let lastFetch = 0;
let timer: ReturnType<typeof setTimeout> | undefined;
let generation = 0;
let fastWanters = 0;
let nullReads = 0;
const listeners = new Set<() => void>();

const hidden = () => typeof document !== "undefined" && document.hidden;

function schedule(delay?: number): void {
  if (timer !== undefined) clearTimeout(timer);
  timer = undefined;
  if (!listeners.size || hidden()) return;
  timer = setTimeout(poll, delay ?? pollIntervalFor(snapshot, fastWanters > 0, nullReads));
}

async function poll(): Promise<void> {
  const mine = ++generation;
  try {
    const data = await getSystemActivity();
    if (mine !== generation) return;
    snapshot = data;
    lastFetch = Date.now();
    nullReads = data.supported && data.totals.cpuPct === null ? nullReads + 1 : 0;
    listeners.forEach((l) => l());
  } catch {
    // Best-effort: a failed read keeps the last snapshot.
  }
  if (mine === generation) schedule();
}

/** Poll now if the last read is older than the cadence wants. */
function pollIfStale(): void {
  const due = pollIntervalFor(snapshot, fastWanters > 0, nullReads);
  if (Date.now() - lastFetch >= due) void poll();
  else schedule(due - (Date.now() - lastFetch));
}

function onVisibility(): void {
  if (hidden()) schedule();
  else pollIfStale();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  if (listeners.size === 1) {
    if (typeof document !== "undefined") document.addEventListener("visibilitychange", onVisibility);
    pollIfStale();
  }
  return () => {
    listeners.delete(listener);
    if (!listeners.size) {
      if (typeof document !== "undefined") {
        document.removeEventListener("visibilitychange", onVisibility);
      }
      generation++;
      schedule();
    }
  };
}

const getSnapshot = () => snapshot;

/** The latest `/api/system/activity` payload (null before the first read).
 *  `fast` asks for the 1 s cadence while it is true. */
export function useSystemActivity(fast = false): SystemActivity | null {
  const data = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  useEffect(() => {
    if (!fast) return;
    fastWanters++;
    pollIfStale();
    return () => {
      fastWanters--;
    };
  }, [fast]);
  return data;
}


/** Test-only: forget the shared snapshot and stop the timer. */
export function resetSystemActivityForTests(): void {
  if (timer !== undefined) clearTimeout(timer);
  timer = undefined;
  snapshot = null;
  lastFetch = 0;
  generation++;
  fastWanters = 0;
  nullReads = 0;
  listeners.clear();
}
