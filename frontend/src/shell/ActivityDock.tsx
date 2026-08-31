// The shell's composer for the status bar's Activity chip (status-bar merge).
// This is the ONE place `<DownloadManager>` is instantiated, and it is the
// sole reason `shell/QueueDock.tsx`, `shell/ModelsDock.tsx` and
// `shell/EnginesDock.tsx` — three separate composers, one per chip that used
// to exist — are gone. `DownloadManager` (platform/ui/DownloadManager.tsx)
// now draws ALL THREE of their row kinds in one panel: Running (the queue's
// rows plus job rows, unchanged from what QueueDock used to hand it),
// Background tasks (running engines) and Models (resident AI models). Its
// header comment has the panel layout; this file only owns getting the three
// DATA SOURCES to it.
//
// WHY THE SOURCES STAY HERE, NOT IN PLATFORM — the same boundary argument
// StatusBar.tsx has always made for QueueDock/RepoUpdatesDock/ModelsDock:
// `explorerUrl` (queue rows) lives in shell/schedule-lib, and the resident-
// model poll (`useAiRuntime`) lives in apps/ai_models/lib — platform may not
// import either (frontend/scripts/check-boundaries.mjs). The running-engines
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
  unloadAiModel,
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
import DownloadManager from "@platform/ui/DownloadManager";
import { publishAiRuntime, useAiRuntime } from "@apps/ai_models/lib/aiRuntime";
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

// lucide `external-link`, drawn here rather than pulled in as a package — the
// house pattern for every icon in this shell.
const ICON_OPEN = (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" aria-hidden="true">
    <path
      d="M15 3h6v6M10 14 21 3M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  </svg>
);

// lucide `loader-circle`, same house rule as ICON_OPEN. It stands where a ✕
// cannot: a claimed entry is a brief state the server will not withdraw, and the
// slot has to look occupied on purpose rather than empty by accident. Decorative
// — the sentence under the title is what carries the meaning.
const ICON_STARTING = (
  <span className="q-spin" aria-hidden="true">
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none">
      <path
        d="M21 12a9 9 0 1 1-6.219-8.56"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
      />
    </svg>
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
    <div className="q-row">
      <div className="q-row-head">
        <span className="q-title" title={entry.message || undefined}>
          {title}
        </span>
        <button
          type="button"
          className="q-open"
          title="Open in Explorer"
          aria-label={`Open ${title} in Explorer`}
          onClick={() => navigateUrl(href)}
        >
          {ICON_OPEN}
        </button>
        {kind === "none" ? (
          ICON_STARTING
        ) : (
          <button
            type="button"
            className="q-x"
            onClick={cancel}
            disabled={busy}
            title={kind === "job" ? "Stop this run" : "Cancel this message"}
            aria-label={kind === "job" ? `Stop ${title}` : `Cancel ${title}`}
          >
            ✕
          </button>
        )}
      </div>
      <div className={"q-status" + (row.role === "live" ? " is-running" : "")}>
        {roleText(row, jobLine)}
      </div>
    </div>
  );
}

export default function ActivityDock({ onFailed }: { onFailed?: (jobs: Job[]) => void } = {}) {
  const { snap, refresh } = useQueue();
  const { engines, refresh: refreshEngines } = useRunningEngines();
  const runtime = useAiRuntime();
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

  const onUnloadModel = async (model: string) => {
    // The response IS a fresh runtime snapshot (`{stopped, ...describe()}`,
    // ai_runtime.py's own route) — publishing it updates every reader (this
    // panel, the sidebar dot) on the click itself, rather than waiting out
    // the next poll tick.
    const result = await unloadAiModel(model);
    publishAiRuntime(result);
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
          <button
            type="button"
            className="q-all"
            onClick={cancelAll}
            disabled={busy}
            title="Cancel every queued message"
          >
            Cancel queued
          </button>
        ) : null,
        note: note ? <div className="q-note">{note}</div> : null,
      }}
      engines={{ engines, onStop: onStopEngine }}
      models={{
        models: runtime.loaded,
        ceilingBytes: runtime.memoryCeilingBytes,
        onUnload: onUnloadModel,
      }}
    />
  );
}
