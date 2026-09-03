// The Activity section of the status bar — ONE panel for every piece of work in
// progress: the long-running operations any page reported (SPEC §36, D244), the
// scheduled messages about to run or running now (the queue slot, rendered by
// the shell), and the running background engines (the engines slot).
//
// WHY IT EXISTS: that work used to be invisible the moment you looked away. A
// page pulling an 8GB model drew its own bar inside itself, and the shell tears
// a page's frame down on every navigation. The record lives on the server
// (fused_render/jobs.py), which is why this survives the reporter's document.
//
// ONE LIFECYCLE IN ONE CONTAINER (Akshil, 2026-08-17): queued → starting →
// running → finished / failed. The first three states are the QUEUE's rows and
// the last is a job row; the queue arrives as a slot because its rows need
// "Open in Explorer", whose answer lives in shell/schedule-lib, and platform
// may not import shell (frontend/scripts/check-boundaries.mjs).
//
// WHICH HALF OWNS A RUN IS TOLD, NOT GUESSED (`queue.drawn`, jobs.ts `jobRows`):
// the slot carries the entry ids its rows cover; this half drops exactly those
// and draws the rest itself. A drawn run is dropped whatever its state, and this
// card hands its snapshot BACK through `onJobs` so the queue half retires a
// finished run against the same evidence (queue-dock-lib `openRows`).
//
// COLLAPSED IS A CHIP, EXPANDED IS A POPOVER (D563/D565/D573): the chip is the
// category name plus one filled/outlined circle; everything else lives in the
// panel. Nothing about the fold is persisted (D603): every section starts
// collapsed on every load; a panel opens on a user press, or transiently for a
// job arrival (`lib/autoExpand.ts`), one at a time across the bar
// (`lib/exclusiveSection.ts`, D582).
//
// Cancel is a REQUEST, not a kill (jobs.py `request_cancel`): the ✕ sets a flag
// the reporting page reads on its next tick, so the row says "Cancelling…"
// until the work actually stops.
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useAutoExpandOnNew } from "@platform/lib/autoExpand";
import { useExclusiveSection } from "@platform/lib/exclusiveSection";
import {
  cancelJob,
  clearableCount,
  clearFinishedJobs,
  dismissJob,
  fetchJobs,
  isRunning,
  jobAmount,
  jobFraction,
  jobRows,
  inFlightJobs,
  mergedRows,
  jobsAfterClear,
  jobStatusLine,
  pollInterval,
  JOB_PING_KEY,
  SCHEDULE_JOB_PREFIX,
  type Job,
  type QueueCount,
} from "@platform/lib/jobs";
import { repoName } from "@platform/lib/format";
import type { RunningEngine } from "@platform/lib/api";
import { cn } from "@platform/lib/utils";
import { bucketFill } from "@platform/ui/status-colors";
import { Button } from "@platform/shadcn/ui/button";
import {
  DockEmpty,
  DockFooter,
  DockRows,
  DockSection,
  StatusBarSection,
} from "@platform/ui/statusbar/StatusBarSection";
import {
  DockAction,
  DockDismiss,
  DockLine,
  DockRow,
  DockRowHead,
  DockTitle,
} from "@platform/ui/statusbar/DockRow";

// Poll /api/jobs, adapting the cadence to whether anything is live, and poll
// IMMEDIATELY when another same-origin document says it just reported (the
// storage ping). The ping is only an optimisation: a reporter with no JS (a
// Python worker) writes none, so the idle poll is the floor.
function useJobs(): {
  /** Has /api/jobs answered once? `jobs` starts `[]` and stays `[]` on an idle
   *  machine, so the list cannot tell "not asked yet" from "genuinely nothing"
   *  — the distinction `useAutoExpandOnNew` needs (D574 bug 2). */
  settled: boolean;
  jobs: Job[];
  refresh: () => void;
  patch: (fn: (jobs: Job[]) => Job[]) => void;
} {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [settled, setSettled] = useState(false);
  const jobsRef = useRef<Job[]>(jobs);
  jobsRef.current = jobs;
  const pollRef = useRef<() => void>(() => {});
  const lastRunningAtRef = useRef<number>(-Infinity);
  // Bumped by every mutation — a response issued BEFORE it describes a list
  // that no longer exists, so painting it flicks a dismissed row back.
  const epochRef = useRef(0);

  useEffect(() => {
    let disposed = false;
    let timer: number | undefined;
    let inFlight = false;
    // A read asked for while one was already in flight — the one that gets
    // dropped is almost always the one that matters (every mutation asks).
    let queued = false;

    const schedule = (ms: number) => {
      window.clearTimeout(timer);
      timer = window.setTimeout(poll, ms);
    };

    const scheduleFor = (jobs: Job[]) => {
      const now = Date.now();
      if (jobs.some(isRunning)) lastRunningAtRef.current = now;
      schedule(pollInterval(jobs, now - lastRunningAtRef.current));
    };

    async function poll() {
      if (document.visibilityState === "hidden") {
        scheduleFor(jobsRef.current);
        return;
      }
      if (inFlight) {
        queued = true;
        return;
      }
      inFlight = true;
      const at = epochRef.current;
      try {
        const snapshot = await fetchJobs();
        if (disposed) return;
        setSettled(true);
        if (at === epochRef.current) {
          setJobs(snapshot.jobs);
          scheduleFor(snapshot.jobs);
        } else {
          scheduleFor(jobsRef.current);
        }
      } catch {
        // The server being unreachable is ServerStatusBanner's story — keep
        // the last list and retry at the idle cadence.
        if (!disposed) scheduleFor(jobsRef.current);
      } finally {
        inFlight = false;
        if (queued && !disposed) {
          queued = false;
          poll();
        }
      }
    }

    pollRef.current = () => {
      if (disposed) return;
      epochRef.current += 1;
      poll();
    };
    poll();

    const onPing = (e: StorageEvent) => {
      if (e.key === JOB_PING_KEY) pollRef.current();
    };
    const onVisible = () => {
      if (document.visibilityState === "visible") pollRef.current();
    };
    window.addEventListener("storage", onPing);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      disposed = true;
      window.clearTimeout(timer);
      window.removeEventListener("storage", onPing);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);

  const refresh = useCallback(() => pollRef.current(), []);

  // Apply a change the SERVER has already confirmed without waiting for a read:
  // a dismiss that answered 200 must remove the row now, not on the next poll.
  const patch = useCallback((fn: (jobs: Job[]) => Job[]) => {
    epochRef.current += 1;
    setJobs(fn);
  }, []);

  return { jobs, settled, refresh, patch };
}

function Bar({ job }: { job: Job }) {
  const fraction = jobFraction(job);
  const fill =
    job.state === "error"
      ? bucketFill.red
      : job.state === "done"
        ? bucketFill.green
        : job.stalled
          ? bucketFill.orange
          : "bg-primary";
  // No fraction and still running = indeterminate: a pulsing track rather than a
  // width parked at an invented percentage, which reads as frozen.
  const indeterminate = fraction === null && isRunning(job) && !job.stalled;
  if (fraction === null && !indeterminate) return null;
  return (
    <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-muted">
      <div
        className={cn(
          "h-full rounded-full motion-safe:transition-[width] motion-safe:duration-150",
          fill,
          indeterminate && "w-full opacity-50 motion-safe:animate-pulse",
        )}
        // `data-indeterminate` is the DOM-observable contract: a headless test
        // cannot see an animation, but it can see which mode the bar is in.
        data-indeterminate={indeterminate ? "1" : undefined}
        style={indeterminate ? undefined : { width: `${(fraction as number) * 100}%` }}
      />
    </div>
  );
}

// ---- Engine rows (status-bar merge) ----------------------------------------
// Formerly shell/EnginesDock.tsx. Legal in platform: the rows need only
// `RunningEngine` and a mutation callback; the poll that feeds them stays in
// shell/ActivityDock.tsx.

/** A useful NAME for an engine row: the folder's basename for a background
 *  app, the module for one with no folder recorded, the id for a template
 *  engine. Pure and exported so it is testable without a render. */
export function engineLabel(engine: RunningEngine): string {
  if (engine.folder) {
    const parts = engine.folder.split(/[/\\]/).filter(Boolean);
    if (parts.length > 0) return parts[parts.length - 1];
  }
  return engine.module || engine.engine_id;
}

function EngineRow({
  engine,
  onStop,
}: {
  engine: RunningEngine;
  onStop: (engineId: string) => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  const stop = async () => {
    setBusy(true);
    setFailure(null);
    try {
      await onStop(engine.engine_id);
    } catch {
      setFailure("Could not stop — check your connection and retry.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <DockRow>
      <DockRowHead>
        <DockTitle token title={engine.folder || engine.engine_id}>
          {engineLabel(engine)}
        </DockTitle>
        <DockAction onClick={stop} disabled={busy}>
          {busy ? "Stopping…" : "Stop"}
        </DockAction>
      </DockRowHead>
      {failure && <DockLine>{failure}</DockLine>}
    </DockRow>
  );
}

/** The `engines` slot: plain data plus the one mutation the row needs, handed
 *  in by shell/ActivityDock.tsx. Ids are for occupancy only (`alsoDrawn`) — an
 *  engine arriving must never itself pop the panel open (D587's rule). */
export interface EnginesSlot {
  engines: RunningEngine[];
  onStop: (engineId: string) => Promise<void>;
}

// Exported for RepoUpdatesDock (a failed job draws with this row, D586).
export function JobRow({
  job,
  onChanged,
  onPatch,
  cancelFn = cancelJob,
  dismissFn = dismissJob,
}: {
  job: Job;
  onChanged: () => void;
  onPatch: (fn: (jobs: Job[]) => Job[]) => void;
  /** Test seam only — real callers get the real calls by default. */
  cancelFn?: (id: string) => Promise<Job>;
  dismissFn?: (id: string) => Promise<{ dismissed: string }>;
}) {
  const [busy, setBusy] = useState(false);
  // A REJECTED cancel/dismiss must say so, not vanish (D572): a row-scoped
  // line, not a toast, since the row is what the user is looking at.
  const [failure, setFailure] = useState<string | null>(null);
  const running = isRunning(job);
  const fraction = jobFraction(job);
  const amount = jobAmount(job);
  const status = jobStatusLine(job);
  // The progress facts together, dot-joined (D598); a failure takes the line
  // over and gets no amount appended.
  const statusLine = failure ?? [status, amount].filter(Boolean).join(" · ");

  // Two controls, one meaning each: a text `Cancel` for a running, cancellable
  // row; the ✕ glyph is reserved for dismiss. A stalled row dismisses rather
  // than cancels — there is nobody left to hear a cancel request.
  const canCancel = running && job.cancellable && !job.cancel_requested && !job.stalled;
  const canDismiss = !running || job.stalled;

  const cancel = async () => {
    setBusy(true);
    setFailure(null);
    try {
      await cancelFn(job.id);
      onPatch((js) => js.map((j) => (j.id === job.id ? { ...j, cancel_requested: true } : j)));
    } catch {
      setFailure("Could not cancel — check your connection and retry.");
    } finally {
      setBusy(false);
      onChanged();
    }
  };

  const dismiss = async () => {
    setBusy(true);
    setFailure(null);
    try {
      await dismissFn(job.id);
      onPatch((js) => js.filter((j) => j.id !== job.id));
    } catch {
      setFailure("Could not dismiss — check your connection and retry.");
    } finally {
      setBusy(false);
      onChanged();
    }
  };

  // Belt-and-braces beside `isVanishedOnSuccess`: a scheduled run's outcome row
  // (`sys:schedule:*`) is deliberately drawn; every other "done" vanishes.
  if (job.state === "done" && !job.id.startsWith(SCHEDULE_JOB_PREFIX)) return null;

  return (
    <DockRow dimmed={job.stalled}>
      <DockRowHead>
        <DockTitle title={job.page || undefined}>{job.title}</DockTitle>
        {fraction !== null && running && (
          <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
            {Math.round(fraction * 100)}%
          </span>
        )}
        {canCancel && (
          <DockAction onClick={cancel} disabled={busy} title="Cancel" aria-label={`Cancel ${job.title}`}>
            Cancel
          </DockAction>
        )}
        {canDismiss && (
          <DockDismiss onClick={dismiss} disabled={busy} title="Dismiss" aria-label={`Dismiss ${job.title}`} />
        )}
      </DockRowHead>
      {/* The model on its own line (D596), suppressed when it just repeats the
          title; the MODEL name only, full id on hover. */}
      {job.model && job.model !== job.title && (
        <DockLine clamp={1} title={job.model}>
          {repoName(job.model)}
        </DockLine>
      )}
      <Bar job={job} />
      {statusLine && <DockLine>{statusLine}</DockLine>}
    </DockRow>
  );
}

/**
 * The queue half of this card, handed in by the shell — data and nodes, not a
 * component: the count has to reach the ONE header and the rows the ONE list,
 * and the rows can only be built in shell (they speak `explorerUrl`).
 */
export interface QueueSlot extends QueueCount {
  /** The queue's rows, already rendered, in lifecycle order. */
  rows: ReactNode;
  /** Which scheduled runs those rows cover, by entry id — so the job half can
   *  drop exactly them. An EMPTY list means "this half draws nothing". */
  drawn: string[];
  /** This card's FULL job snapshot, handed back on every poll so the queue half
   *  retires a finished run promptly (queue-dock-lib `openRows`). */
  onJobs?: (jobs: Job[]) => void;
  /** "Cancel queued" — only ever rendered inside the expanded panel. */
  cancelAll?: ReactNode;
  /** What a cancel actually did, including the half that was refused. */
  note?: ReactNode;
}

// A successful job vanishes from this card entirely — EXCEPT a scheduled run's
// own outcome row (`sys:schedule:*`), which survives until jobs.py's retention
// sweeps it (Akshil, 2026-08-21: "a run appears, works, and vanishes
// mid-sentence"). Component-local: other readers of the registry need a "done"
// record to keep existing.
function isVanishedOnSuccess(job: Job): boolean {
  return job.state === "done" && !job.id.startsWith(SCHEDULE_JOB_PREFIX);
}

// The pure, props-in half: no polling, no network.
export function DownloadManagerView({
  reported,
  ready,
  initialCollapsed,
  queue,
  engines,
  refresh,
  patch,
}: {
  reported: Job[];
  /** TEST SEAM — the fold's initial value. Real callers omit it: sections
   *  ALWAYS start collapsed (D603). */
  initialCollapsed?: boolean;
  /** Has the first /api/jobs read landed (autoExpand.ts's `ready`)? */
  ready?: boolean;
  queue?: QueueSlot;
  /** The Background tasks section — data only. */
  engines?: EnginesSlot;
  refresh: () => void;
  patch: (fn: (jobs: Job[]) => Job[]) => void;
}) {
  const [collapsed, setCollapsed] = useState(initialCollapsed ?? true);
  // Hand this poll's snapshot back to the queue half — in an effect, since it
  // sets state in the parent. The FULL snapshot: `isVanishedOnSuccess` decides
  // what THIS card draws, not what the queue half is told about.
  const onJobs = queue?.onJobs;
  useEffect(() => {
    onJobs?.(reported);
  }, [onJobs, reported]);
  // Everything the poll returned MINUS the runs the queue's rows draw (told,
  // never assumed), minus vanished successes, minus failures (D586: an `error`
  // row is not work in progress — it moves to Notifications). `mergedRows`
  // runs first on the full snapshot (it needs the `waiting_for` waiter present).
  const jobs = inFlightJobs(
    jobRows(mergedRows(reported), queue?.drawn).filter((j) => !isVanishedOnSuccess(j)),
  );
  const count: QueueCount = { waiting: queue?.waiting ?? 0, running: queue?.running ?? 0 };
  const queued = count.waiting + count.running;

  // Only job arrivals may OPEN the panel; queue rows and engine rows hold it
  // open (`alsoDrawn`, prefixed per source so ids cannot collide).
  const { autoOpen, autoClose, acknowledge, forceClose } = useAutoExpandOnNew(
    jobs.map((j) => `job:${j.id}`),
    collapsed,
    ready,
    {
      alsoDrawn: [
        ...(queue?.drawn ?? []).map((id) => `queue:${id}`),
        ...(engines?.engines ?? []).map((e) => `engine:${e.engine_id}`),
      ],
    },
  );
  // The preference, overridden in EITHER direction by whichever transient flag
  // is standing (D580); a drained list beats a stale auto-open.
  const open = autoClose ? false : !collapsed || autoOpen;

  // ONE panel at a time across the whole bar (D582) — only ever closes, transiently.
  useExclusiveSection("activity", open, forceClose);

  const engineCount = engines?.engines.length ?? 0;
  const idle = jobs.length === 0 && queued === 0 && engineCount === 0;
  const clearable = clearableCount(jobs);

  // ONE unified toggle: acts on what the user SEES, writes the preference only
  // if the preference is what disagrees (D574/D580).
  const toggle = () => {
    const wantOpen = !open;
    acknowledge();
    if (collapsed === wantOpen) setCollapsed(!wantOpen);
  };

  const clear = async () => {
    try {
      await clearFinishedJobs();
      patch((js) => jobsAfterClear(js));
    } catch {
      /* nothing applied locally — the refresh below is the source of truth */
    }
    refresh();
  };

  // THE DOT ANSWERS "IS THERE WORK RIGHT NOW": jobs running or queued fill it; a
  // running engine — persistent state — does not, though the panel shows it.
  const runningCount = jobs.length + queued;
  const runningVisible = jobs.length > 0 || queued > 0;
  const showHeadings = (runningVisible ? 1 : 0) + (engineCount > 0 ? 1 : 0) > 1;

  return (
    <StatusBarSection
      label="Activity"
      on={runningCount > 0}
      dotLabel={runningCount > 0 ? "jobs running" : "no jobs running"}
      idle={idle}
      open={open}
      hasRows={!idle}
      title={open ? "Hide activity" : "Show activity"}
      onToggle={toggle}
      // Outside press / Escape: transient only, never a write to the preference.
      onDismiss={forceClose}
    >
      {idle ? (
        <DockEmpty>No activity</DockEmpty>
      ) : (
        <>
          {runningVisible && (
            <DockSection heading={showHeadings ? "Running" : undefined}>
              {/* ONE list in lifecycle order: queue rows first, job rows under
                  them — where the same run lands once its turn has ended. */}
              <DockRows>
                {queue?.rows}
                {jobs.map((job) => (
                  <JobRow key={job.id} job={job} onChanged={refresh} onPatch={patch} />
                ))}
              </DockRows>
              {queue?.note}
              {/* A footer, not a header (D602); plurality-gated on Clear (D604). */}
              {(queue?.cancelAll || clearable > 1) && (
                <DockFooter>
                  {queue?.cancelAll}
                  {clearable > 1 && (
                    <Button variant="outline" size="xs" onClick={clear} title="Dismiss finished">
                      Clear
                    </Button>
                  )}
                </DockFooter>
              )}
            </DockSection>
          )}
          {engineCount > 0 && (
            <DockSection heading={showHeadings ? "Background tasks" : undefined}>
              <DockRows>
                {engines!.engines.map((e) => (
                  <EngineRow key={e.engine_id} engine={e} onStop={engines!.onStop} />
                ))}
              </DockRows>
            </DockSection>
          )}
        </>
      )}
    </StatusBarSection>
  );
}

export default function DownloadManager({
  queue,
  engines,
}: {
  queue?: QueueSlot;
  engines?: EnginesSlot;
}) {
  const { jobs: reported, settled, refresh, patch } = useJobs();
  return (
    <DownloadManagerView
      reported={reported}
      ready={settled}
      queue={queue}
      engines={engines}
      refresh={refresh}
      patch={patch}
    />
  );
}
