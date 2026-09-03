// The shell's composer for the status bar's Activity chip. This is the ONE
// place `<DownloadManager>` is instantiated, and it is the sole reason
// `shell/QueueDock.tsx` and `shell/EnginesDock.tsx` — two separate composers,
// one per chip that used to exist — are gone. `DownloadManager`
// (platform/ui/DownloadManager.tsx) draws the Running section (job rows) and
// the Background tasks section (running engines). Its header comment has the
// panel layout; this file only owns getting the two DATA SOURCES to it.
// (Models — resident AI models — made this same trip during the merge and
// then split back out into its own chip, `shell/ModelsDock.tsx`, once its own
// filled/outlined dot needed to answer a question this chip's shared "is
// there work right now" dot could not.)
//
// A SCHEDULED MESSAGE'S OWN RUN DRAWS NO ROW HERE AT ALL (D661, user: "a task
// is not something I even want in the activity. that was added
// unintentionally"). This file used to poll `/api/schedule-queue` itself and
// hand `DownloadManager` a `queue` slot of scheduled-message rows merged in
// beside the job rows — that whole poll, `useQueue`, `QueueRowView`, and
// `shell/queue-dock-lib.ts` (its pure row-shaping half) are DELETED, not
// merely unused. `jobs.ts`'s `jobRows` now excludes `sys:schedule:*` jobs
// unconditionally (no more "exempt only while queued" carve-out), so a
// scheduled run cannot draw a row here no matter what state it is in. The
// "Task finished:"/"Task failed:" toast (platform/lib/schedule-toast.ts,
// consuming `useScheduleEvents` in App.tsx) is the one surface for these now;
// `fused_render/schedule.py`'s own `_emit`/`_report` are untouched, since its
// poll loop still reads its own report back to notice a live cancel request.
//
// A KNOWN LOSS FROM THIS (documented prominently in DECISIONS.md): the old
// queue card could cancel an ALREADY-RUNNING scheduled turn (`cancelJob` on
// its `sys:schedule:` job id) — `shell/Scheduled.tsx` only cancels an
// entry that has not been sent yet. There is currently no surface for
// stopping a turn already in flight. Accepted rather than worked around: the
// user's own words were that this row should not exist at all, and rebuilding
// a narrower "cancel only" control just to keep that one capability would be
// reintroducing the exact row they asked to have removed.
//
// WHY THE ENGINE SOURCE STAYS HERE, NOT IN PLATFORM — the same boundary
// argument StatusBar.tsx has always made for RepoUpdatesDock/ModelsDock:
// `DownloadManager`'s job is to RENDER rows from data, not to fetch it, and
// this component is the shell's one place that fetches for it.
import { useCallback, useEffect, useRef, useState } from "react";
import { getRunningEngines, stopEngine, type RunningEngine } from "@platform/lib/api";
import { terminalNotifications, type Job } from "@platform/lib/jobs";
import { pushToast } from "@platform/lib/toast";
import DownloadManager, { engineLabel } from "@platform/ui/DownloadManager";

// Matches the (former) Engines chip's own cadence: a "what is running" readout,
// not progress, so it does not need to tick every second.
const ENGINES_POLL_MS = 10_000;

// How long a `markStopping` marker suppresses the retire toast for (C5 fix).
// Comfortably more than one `ENGINES_POLL_MS` round trip, so the very next
// poll after a Stop click — the case this exists for — still finds its
// marker. NOT forever: a marker used to be consumed only by the first later
// snapshot that dropped the id, so a rejected `stopEngine()` call, or an
// engine a `main =` app's `restart()` revives, left the marker standing with
// nothing to consume it — and the id's eventual GENUINE idle retirement,
// possibly minutes later, silently ate the D664 toast that retirement earned.
// Expiring the marker bounds the suppression to the window the user's own
// click could plausibly still be resolving in, so a later real retirement is
// never mistaken for an echo of that one click.
const STOPPING_GRACE_MS = 30_000;

/**
 * Which engines from `prev` disappeared in `next` and were NOT a user-
 * initiated stop — the set D664's toast fires for. Pure and exported so C9's
 * gap (D664 shipped with no test at all) can be closed without mounting the
 * whole poll effect: `stopping` is mutated in place exactly as the poll loop
 * mutates its own ref, consuming a marker the moment its window is checked
 * (whether or not it still suppressed anything) so a stale one never lingers
 * to swallow a later retirement.
 */
export function retiredEngines(
  prev: RunningEngine[],
  next: RunningEngine[],
  stopping: Map<string, number>,
  now: number,
): RunningEngine[] {
  const nextIds = new Set(next.map((e) => e.engine_id));
  const retired: RunningEngine[] = [];
  for (const p of prev) {
    if (nextIds.has(p.engine_id)) continue;
    const stoppedAt = stopping.get(p.engine_id);
    if (stoppedAt !== undefined) {
      stopping.delete(p.engine_id);
      if (now - stoppedAt < STOPPING_GRACE_MS) continue; // user-initiated, within grace
    }
    retired.push(p);
  }
  return retired;
}

function useRunningEngines(): {
  engines: RunningEngine[];
  refresh: () => void;
  /** Mark an engine as being stopped BY THE USER, so the next snapshot that no
   *  longer carries it is not read as an idle retirement (below). Exposed
   *  from the hook because the stop request and the poll that will notice the
   *  engine gone are two different call sites. */
  markStopping: (engineId: string) => void;
} {
  const [engines, setEngines] = useState<RunningEngine[]>([]);
  const pollRef = useRef<() => void>(() => {});
  // Engine ids a user-initiated Stop is in flight for — read once, on the
  // NEXT snapshot that drops them, then discarded. A `Set` mutated in place
  // rather than state: it is consulted only inside the poll effect below and
  // must never itself trigger a render.
  const stoppingRef = useRef<Map<string, number>>(new Map());
  // The PREVIOUS snapshot itself, so a snapshot that drops an engine can tell
  // an idle retirement (ENGINE-STOP TOAST, below) from ordinary churn. A ref,
  // not the `engines` state variable: the poll effect below runs once (empty
  // deps) and would otherwise always see the FIRST render's stale closure.
  const prevEnginesRef = useRef<RunningEngine[]>([]);
  const sawFirst = useRef(false);

  useEffect(() => {
    let disposed = false;
    let timer = 0;
    // Only the newest invocation may schedule — the same generation guard
    // `useRepoUpdates`/the old EnginesDock poll carry: `clearTimeout` cancels a
    // PENDING timer, but a `refresh()` landing while an earlier poll awaits
    // leaves both in flight, and each would assign `timer` on the way out,
    // leaking one unclearable chain.
    let generation = 0;
    const poll = async () => {
      const mine = ++generation;
      window.clearTimeout(timer);
      try {
        const data = await getRunningEngines();
        if (!disposed && mine === generation) {
          const next = data.engines || [];
          // AN ENGINE RETIRED ON ITS OWN GETS A TOAST (D664): the only way to
          // learn a background daemon/worker went away idle is to notice it
          // missing from consecutive snapshots — nothing calls this out as an
          // event server-side. Skipped on the very FIRST snapshot (nothing to
          // diff against yet — every engine already running would otherwise
          // read as having just retired) and skipped for any id this hook was
          // told a user just stopped themselves.
          if (sawFirst.current) {
            for (const prev of retiredEngines(prevEnginesRef.current, next, stoppingRef.current, Date.now())) {
              pushToast({ msg: `${engineLabel(prev)} retired (idle)`, tone: "info" });
            }
          }
          sawFirst.current = true;
          prevEnginesRef.current = next;
          setEngines(next);
        }
      } catch {
        // Best-effort: a failed read leaves the last snapshot standing.
      }
      if (!disposed && mine === generation) timer = window.setTimeout(poll, ENGINES_POLL_MS);
    };
    pollRef.current = poll;
    poll();
    return () => {
      disposed = true;
      window.clearTimeout(timer);
    };
  }, []);

  const refresh = useCallback(() => pollRef.current(), []);
  const markStopping = useCallback((engineId: string) => {
    stoppingRef.current.set(engineId, Date.now());
  }, []);
  return { engines, refresh, markStopping };
}

export default function ActivityDock({
  onTerminalJobs,
}: { onTerminalJobs?: (jobs: Job[]) => void } = {}) {
  const { engines, refresh: refreshEngines, markStopping } = useRunningEngines();

  // TERMINAL JOBS, ON THEIR WAY FROM Activity TO Notifications (D586,
  // broadened by D662 to every terminal state, not only `error`).
  // `DownloadManager` already hands this the FULL, unfiltered snapshot on
  // every poll (`onJobsReported`); this only needs to re-derive the terminal
  // subset and call up when the id SET actually changes, so a poll that finds
  // nothing new does not re-render the shell.
  //
  // `terminalNotifications` (jobs.ts) is `mergedRows` then `jobRows` then
  // `terminalJobs` — see its own doc for why that order matters (a scheduled
  // run's own job draws no row in Activity in any state, D661/`isScheduleJob`,
  // and a render merged with a shared model load must not surface the
  // load's completion as a second Notifications entry, SPEC §36).
  const onTerminalRef = useRef(onTerminalJobs);
  onTerminalRef.current = onTerminalJobs;
  const terminalIdsRef = useRef("");
  const onJobsReported = useCallback((next: Job[]) => {
    const terminal = terminalNotifications(next);
    const key = terminal.map((j) => j.id).join(" ");
    if (key === terminalIdsRef.current) return;
    terminalIdsRef.current = key;
    onTerminalRef.current?.(terminal);
  }, []);

  const onStopEngine = async (engineId: string) => {
    markStopping(engineId);
    await stopEngine(engineId);
    refreshEngines();
  };

  return (
    <DownloadManager
      engines={{ engines, onStop: onStopEngine }}
      onJobsReported={onJobsReported}
    />
  );
}
