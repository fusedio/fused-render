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
// A SCHEDULED MESSAGE'S OWN RUN DRAWS NO ROW HERE AT ALL (D634, user: "a task
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
import { terminalJobs, type Job } from "@platform/lib/jobs";
import { pushToast } from "@platform/lib/toast";
import DownloadManager, { engineLabel } from "@platform/ui/DownloadManager";

// Matches the (former) Engines chip's own cadence: a "what is running" readout,
// not progress, so it does not need to tick every second.
const ENGINES_POLL_MS = 10_000;

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
  const stoppingRef = useRef<Set<string>>(new Set());
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
          const nextIds = new Set(next.map((e) => e.engine_id));
          // AN ENGINE RETIRED ON ITS OWN GETS A TOAST (D635): the only way to
          // learn a background daemon/worker went away idle is to notice it
          // missing from consecutive snapshots — nothing calls this out as an
          // event server-side. Skipped on the very FIRST snapshot (nothing to
          // diff against yet — every engine already running would otherwise
          // read as having just retired) and skipped for any id this hook was
          // told a user just stopped themselves.
          if (sawFirst.current) {
            for (const prev of prevEnginesRef.current) {
              if (nextIds.has(prev.engine_id)) continue;
              if (stoppingRef.current.delete(prev.engine_id)) continue; // user-initiated
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
    stoppingRef.current.add(engineId);
  }, []);
  return { engines, refresh, markStopping };
}

export default function ActivityDock({
  onTerminalJobs,
}: { onTerminalJobs?: (jobs: Job[]) => void } = {}) {
  const { engines, refresh: refreshEngines, markStopping } = useRunningEngines();

  // TERMINAL JOBS, ON THEIR WAY FROM Activity TO Notifications (D586,
  // broadened by D635 to every terminal state, not only `error`).
  // `DownloadManager` already hands this the FULL, unfiltered snapshot on
  // every poll (`onJobsReported`); this only needs to re-derive the terminal
  // subset and call up when the id SET actually changes, so a poll that finds
  // nothing new does not re-render the shell.
  const onTerminalRef = useRef(onTerminalJobs);
  onTerminalRef.current = onTerminalJobs;
  const terminalIdsRef = useRef("");
  const onJobsReported = useCallback((next: Job[]) => {
    const terminal = terminalJobs(next);
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
