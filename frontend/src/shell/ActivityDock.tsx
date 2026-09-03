// The shell's composer for the status bar's Activity chip (status-bar merge,
// later revised to split Models back out into its own chip). This is the ONE
// place `<DownloadManager>` is instantiated, and it is the sole reason
// `shell/QueueDock.tsx` and `shell/EnginesDock.tsx` — two separate composers,
// one per chip that used to exist — are gone. `DownloadManager`
// (platform/ui/DownloadManager.tsx) now draws BOTH their row kinds in one
// panel: Running (the queue's rows plus job rows, unchanged from what
// QueueDock used to hand it) and Background tasks (running engines). Its
// header comment has the panel layout; this file only owns getting the two
// DATA SOURCES to it. (Models — resident AI models — made this same trip
// during the merge and then split back out into its own chip,
// `shell/ModelsDock.tsx`, once its own filled/outlined dot needed to answer a
// question this chip's shared "is there work right now" dot could not.)
//
// WHY THE SOURCES STAY HERE, NOT IN PLATFORM — the same boundary argument
// StatusBar.tsx has always made for QueueDock/RepoUpdatesDock/ModelsDock:
// `explorerUrl` (queue rows) lives in shell/schedule-lib, and platform may
// not import it (frontend/scripts/check-boundaries.mjs). The running-engines
// poll itself has no such dependency (`getRunningEngines`/`stopEngine` are
// already platform/lib/api), but it is grouped here anyway rather than split
// across a shell file and a platform file for one poll: `DownloadManager`'s
// job is to RENDER rows from data, not to fetch it, and this component is
// the shell's one place that fetches for it.
import { useCallback, useEffect, useRef, useState } from "react";
import {
  cancelQueued,
  getRunningEngines,
  getScheduleQueue,
  stopEngine,
  type RunningEngine,
  type ScheduledMessage,
} from "@platform/lib/api";
import {
  failedJobs,
  cancelJob,
  isRunning,
  jobStatusLine,
  SCHEDULE_JOB_PREFIX,
  type Job,
} from "@platform/lib/jobs";
import { navigateUrl } from "@platform/lib/router";
import { ExternalLink, LoaderCircle } from "lucide-react";
import { cn } from "@platform/lib/utils";
import { bucketText } from "@platform/ui/status-colors";
import { Button } from "@platform/shadcn/ui/button";
import DownloadManager from "@platform/ui/DownloadManager";
import { DockDismiss, DockLine, DockRow, DockRowHead, DockTitle } from "@platform/ui/statusbar/DockRow";
import { cancelOutcome, explorerUrl, firstLine } from "@shell/schedule-lib";
import {
  drawnIds,
  openRows,
  queueCount,
  queueRows,
  roleText,
  rowCancelKind,
  scheduleRunsEnded,
  scheduleRunsStarted,
  showCancelAll,
  type QueueRow,
} from "@shell/queue-dock-lib";
import { pokeTasks } from "@shell/tasksPulse";

// Fast enough that a row appears near the moment it comes due, slow enough to be
// a permanent background poll in every shell. The queue moves on the scheduler's
// own tick, so a second of lag costs nothing.
const QUEUE_POLL_MS = 6000;
// Matches the (former) Engines chip's own cadence: a "what is running" readout,
// not progress, so it does not need to tick every second.
const ENGINES_POLL_MS = 10_000;

interface Snapshot {
  queued: ScheduledMessage[];
  running: ScheduledMessage[];
  live: ScheduledMessage[];
}

const EMPTY: Snapshot = { queued: [], running: [], live: [] };

// `live` is the router's third list (server/routers/schedule.py) — entries whose
// turn is still in flight. api.ts's declared return type predates it; the cast is
// narrow and the field is optional, so a server that does not send it degrades to
// a card without live rows rather than to a crash.
type QueuePayload = Awaited<ReturnType<typeof getScheduleQueue>> & {
  live?: ScheduledMessage[];
};

function useQueue(): { snap: Snapshot; refresh: () => void } {
  const [snap, setSnap] = useState<Snapshot>(EMPTY);
  const pollRef = useRef<() => void>(() => {});

  useEffect(() => {
    let disposed = false;
    let timer: number | undefined;

    async function poll() {
      // A hidden tab is not being read, and its throttled timers fire in a clump
      // on return anyway — keep the loop alive rather than reading in the dark.
      if (document.visibilityState !== "hidden") {
        try {
          const r = (await getScheduleQueue()) as QueuePayload;
          if (disposed) return;
          setSnap({
            queued: r.queued ?? [],
            running: r.running ?? [],
            live: r.live ?? [],
          });
        } catch {
          // The LAST snapshot stays — an unreadable queue is not an empty one.
        }
      }
      if (!disposed) timer = window.setTimeout(poll, QUEUE_POLL_MS);
    }

    pollRef.current = () => {
      if (disposed) return;
      window.clearTimeout(timer);
      poll();
    };
    poll();
    const onVisible = () => {
      if (document.visibilityState === "visible") pollRef.current();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      disposed = true;
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);

  const refresh = useCallback(() => pollRef.current(), []);
  return { snap, refresh };
}

function useRunningEngines(): { engines: RunningEngine[]; refresh: () => void } {
  const [engines, setEngines] = useState<RunningEngine[]>([]);
  const pollRef = useRef<() => void>(() => {});

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
        if (!disposed && mine === generation) setEngines(data.engines || []);
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
  return { engines, refresh };
}

// Stands where a ✕ cannot: a claimed entry is a brief state the server will not
// withdraw, and the slot has to look occupied on purpose rather than empty by
// accident. Decorative — the sentence under the title carries the meaning. The
// spin is guarded (motion-safe:) and parks still under reduced motion.
const ICON_STARTING = (
  <span className="ml-auto inline-flex size-6 shrink-0 items-center justify-center text-muted-foreground" aria-hidden="true">
    <LoaderCircle size={12} className="motion-safe:animate-spin" />
  </span>
);

function QueueRowView({
  row,
  jobLine,
  onDone,
  onNote,
}: {
  row: QueueRow;
  jobLine: string;
  onDone: () => void;
  onNote: (msg: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const { entry } = row;
  const kind = rowCancelKind(row);
  const href = explorerUrl(entry.target, entry.claude_session_id || entry.session_id || "");

  const cancel = async () => {
    if (kind === "none") return;
    setBusy(true);
    try {
      if (kind === "job") {
        await cancelJob(SCHEDULE_JOB_PREFIX + entry.id);
        onNote("Stopping…");
      } else {
        const r = await cancelQueued([entry.id]);
        const said = cancelOutcome(r.cancelled ?? [], r.refused ?? []);
        if (said) onNote(said);
      }
    } catch {
      onNote("That could not be cancelled — try the Tasks page.");
    } finally {
      setBusy(false);
      onDone();
    }
  };

  const title = firstLine(entry.message) || "Scheduled message";
  return (
    <DockRow>
      <DockRowHead>
        {/* The title does not grow: the controls FOLLOW the text (D573), and
            only the trailing ✕ takes the slack (D609). */}
        <DockTitle token className="flex-none max-w-full" title={entry.message || undefined}>
          {title}
        </DockTitle>
        {/* The reason this card exists: a run parked on a permission prompt was
            unreachable. The icon is the row's way THERE, beside its way out. */}
        <Button
          variant="ghost"
          size="icon-xs"
          className="shrink-0 text-muted-foreground hover:text-foreground"
          title="Open in Explorer"
          aria-label={`Open ${title} in Explorer`}
          onClick={() => navigateUrl(href)}
        >
          <ExternalLink />
        </Button>
        {kind === "none" ? (
          ICON_STARTING
        ) : (
          <DockDismiss
            onClick={cancel}
            disabled={busy}
            title={kind === "job" ? "Stop this run" : "Cancel this message"}
            aria-label={kind === "job" ? `Stop ${title}` : `Cancel ${title}`}
          />
        )}
      </DockRowHead>
      {/* A live row's sentence wears the app's one "running" treatment: the In
          Progress yellow (status-colors) with the house shimmer, which the
          token sheet guards under prefers-reduced-motion. */}
      <DockLine clamp={2} className={cn(row.role === "live" && [bucketText.yellow, "shimmer-text"])}>
        {roleText(row, jobLine)}
      </DockLine>
    </DockRow>
  );
}

export default function ActivityDock({ onFailed }: { onFailed?: (jobs: Job[]) => void } = {}) {
  const { snap, refresh } = useQueue();
  const { engines, refresh: refreshEngines } = useRunningEngines();
  // The card's own job snapshot, handed up on every one of ITS polls — about
  // once a second while anything is live, against this half's six.
  const [jobs, setJobs] = useState<Job[]>([]);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const prevJobs = useRef<Job[]>([]);
  const sawEcho = useRef(false);
  const sawJobs = useRef(false);
  // FAILURES GO TO NOTIFICATIONS (D586). This half already receives
  // `DownloadManager`'s full, unfiltered snapshot on every poll, so the
  // re-route needs no second poller and no store.
  const onFailedRef = useRef(onFailed);
  onFailedRef.current = onFailed;
  const failedIds = useRef("");
  const forwardFailed = useCallback((next: Job[]) => {
    const failed = failedJobs(next);
    const key = failed.map((j) => j.id).join(" ");
    if (key === failedIds.current) return;
    failedIds.current = key;
    onFailedRef.current?.(failed);
  }, []);

  const onJobs = useCallback((next: Job[]) => {
    forwardFailed(next);
    if (!sawEcho.current) {
      sawEcho.current = true;
      setJobs(next);
      return;
    }
    const news =
      sawJobs.current &&
      (scheduleRunsEnded(prevJobs.current, next) || scheduleRunsStarted(prevJobs.current, next));
    if (news) pokeTasks();
    sawJobs.current = true;
    prevJobs.current = next;
    setJobs(next);
  }, [forwardFailed]);

  const rows = openRows(queueRows(snap.live, snap.running, snap.queued), jobs);
  const lines = new Map<string, string>();
  for (const job of jobs) {
    if (job.id.startsWith(SCHEDULE_JOB_PREFIX) && isRunning(job)) {
      lines.set(job.id.slice(SCHEDULE_JOB_PREFIX.length), jobStatusLine(job) || job.detail);
    }
  }

  const cancelAll = async () => {
    setBusy(true);
    try {
      const r = await cancelQueued("all");
      setNote(cancelOutcome(r.cancelled ?? [], r.refused ?? []));
    } catch {
      setNote("Nothing was cancelled — the queue could not be reached.");
    } finally {
      setBusy(false);
      refresh();
    }
  };

  const onStopEngine = async (engineId: string) => {
    await stopEngine(engineId);
    refreshEngines();
  };

  return (
    <DownloadManager
      queue={{
        ...queueCount(rows),
        drawn: drawnIds(rows),
        onJobs,
        rows: rows.map((row) => (
          <QueueRowView
            key={row.entry.id}
            row={row}
            jobLine={lines.get(row.entry.id) ?? ""}
            onDone={refresh}
            onNote={setNote}
          />
        )),
        cancelAll: showCancelAll(rows) ? (
          <Button
            variant="outline"
            size="xs"
            onClick={cancelAll}
            disabled={busy}
            title="Cancel every queued message"
          >
            Cancel queued
          </Button>
        ) : null,
        note: note ? (
          <div className="border-t border-border px-2.5 py-1.5 text-xs leading-snug text-muted-foreground">{note}</div>
        ) : null,
      }}
      engines={{ engines, onStop: onStopEngine }}
    />
  );
}
