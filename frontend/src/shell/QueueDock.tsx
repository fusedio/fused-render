// The queue half of the ONE bottom-right activity card: work that is about to run
// or running now (Akshil, 2026-08-17).
//
// It used to be a card of its own, stacked directly above the download manager,
// and it is not any more — "this queue and notification thing should be same no?
// why duplicate popups? just replace the queue -> thinking -> done". Same corner,
// same plate, same shape, same kind of thing (work in progress) under two headers
// and two counts, and a scheduled run appeared in the top card while it waited and
// the bottom one once it had finished: one run changing container mid-life. So this
// module no longer draws a card. It polls, it renders ROWS, and it hands them to
// DownloadManager, which owns the plate, the one header, the one count, the one
// list and Clear. The lifecycle is one list now:
//
//     queued → starting → running → finished / failed
//        \______ these rows ______/     \__ a job row __/
//
// WHAT COUNTS AS QUEUED is the whole definition, and it is the server's, not a
// filter invented here: `GET /api/schedule/queue` answers with what is PAST DUE
// and waiting to be claimed. A message scheduled for later today is not queued,
// it is scheduled, and putting it in this card would make Cancel all mean
// something nobody asked for ("cancel my afternoon").
//
// WHY IT EXISTS, in the user's words: a run parked on a permission prompt was
// invisible — there was a row saying "waiting for permission" and no way to find
// out WHERE to go and answer it. So every row here carries Open in Explorer, and
// the link is `explorerUrl`, the app's one answer to that question, rather than a
// path assembled here.
//
// It is also the only place Cancel all now lives: the calendar's Queued strip was
// removed and this replaces the global surface that went with it.
//
// WHY THE SHELL COMPOSES THE CARD instead of platform polling the queue itself:
// `explorerUrl` — and `cancelOutcome`, and `relativeDue` — live in
// shell/schedule-lib, and platform may not import shell
// (frontend/scripts/check-boundaries.mjs). Rather than inject three functions
// downward, the dependency runs the way the boundary already allows: shell imports
// platform's card and fills its `queue` slot with the parts only shell can build.
// Placement is still NotificationHost's, which takes this whole thing as its one
// `activity` entry.
import { useCallback, useEffect, useRef, useState } from "react";
import {
  cancelQueued,
  getScheduleQueue,
  type ScheduledMessage,
} from "@platform/lib/api";
import {
  cancelJob,
  fetchJobs,
  isRunning,
  jobStatusLine,
  SCHEDULE_JOB_PREFIX,
  type Job,
} from "@platform/lib/jobs";
import { navigateUrl } from "@platform/lib/router";
import DownloadManager from "@platform/ui/DownloadManager";
import { cancelOutcome, explorerUrl, firstLine } from "@shell/schedule-lib";
import {
  queueCount,
  queueRows,
  roleText,
  rowCancelKind,
  showCancelAll,
  type QueueRow,
} from "@shell/queue-dock-lib";

// Fast enough that a row appears near the moment it comes due, slow enough to be
// a permanent background poll in every shell. The queue moves on the scheduler's
// own tick, so a second of lag costs nothing.
const POLL_MS = 6000;

// The job registry is where a LIVE turn reports its progress, and its line —
// "waiting for permission", "working · 4210 tokens" — is the most useful string
// in this card, so the row joins it onto the entry by id rather than inventing a
// status of its own. That same job is filtered out of the job rows below
// (`jobRows`), so one run occupies one row in one list.
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

function useQueue(): { snap: Snapshot; jobs: Job[]; refresh: () => void } {
  const [snap, setSnap] = useState<Snapshot>(EMPTY);
  const [jobs, setJobs] = useState<Job[]>([]);
  const pollRef = useRef<() => void>(() => {});

  useEffect(() => {
    let disposed = false;
    let timer: number | undefined;

    async function poll() {
      // A hidden tab is not being read, and its throttled timers fire in a clump
      // on return anyway — keep the loop alive rather than reading in the dark.
      if (document.visibilityState !== "hidden") {
        let live = 0;
        try {
          const r = (await getScheduleQueue()) as QueuePayload;
          if (disposed) return;
          live = (r.live ?? []).length;
          setSnap({
            queued: r.queued ?? [],
            running: r.running ?? [],
            live: r.live ?? [],
          });
        } catch {
          // The LAST snapshot stays. An unreadable queue is not an empty one, and
          // blanking these rows would take a live run off the screen (they are
          // where it lives — the job rows drop it) on one bad probe.
        }
        // Only when there is a live turn to describe. The status line comes from
        // the job registry, which DownloadManager is already polling — asking for
        // it again with nothing running would be a second forever-poll in every
        // shell, bought for a line nothing on screen is waiting to print.
        if (live > 0) {
          try {
            const snapshot = await fetchJobs();
            if (!disposed) setJobs(snapshot.jobs);
          } catch {
            /* a status line goes stale; the row does not disappear */
          }
        } else if (!disposed) {
          setJobs((was) => (was.length ? [] : was));
        }
      }
      if (!disposed) timer = window.setTimeout(poll, POLL_MS);
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
  return { snap, jobs, refresh };
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

function Row({
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
  // Recomputed every render, and the row is re-rendered from a fresh snapshot on
  // every poll — so the control set follows the entry through queued → sending →
  // live rather than being decided once when the row first appeared.
  const kind = rowCancelKind(row);
  // The session the turn landed in first, the one it was told to resume second:
  // a run that has started has a real conversation to open, and one that has not
  // opens the folder with the Claude pane on it — which is where its words are
  // about to arrive. Never assembled here; explorerUrl is the app's one answer.
  const href = explorerUrl(entry.target, entry.claude_session_id || entry.session_id || "");

  const cancel = async () => {
    // No control is drawn for a claimed row; if one somehow fires (the entry got
    // claimed between render and press) do nothing rather than ask for a refusal.
    if (kind === "none") return;
    setBusy(true);
    try {
      if (kind === "job") {
        // The job registry's cancel really stops the run (schedule.py owns the
        // process), unlike a page-owned job where it is only a request.
        await cancelJob(SCHEDULE_JOB_PREFIX + entry.id);
        onNote("Stopping…");
      } else {
        const r = await cancelQueued([entry.id]);
        // Never dropped silently: cancelling races the claim, and an entry that
        // got away comes back refused. cancelOutcome is the app's own wording.
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
          // A claimed entry has no cancel — the server refuses `sending` on
          // purpose — so the slot holds a spinner instead of a dead button: it
          // keeps the row's controls from jumping sideways when the state moves,
          // and says the row is working rather than stuck. The WORDS are in
          // .q-status (roleText), because a title tooltip is not reachable by
          // keyboard and this is the only explanation the row gets.
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
      <div className="q-status">{roleText(row, jobLine)}</div>
    </div>
  );
}

export default function QueueDock() {
  const { snap, jobs, refresh } = useQueue();
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  const rows = queueRows(snap.live, snap.running, snap.queued);
  // Nothing about to run and nothing running means NO ROWS, not an empty state —
  // and whether that leaves a card at all is the card's own question now, because
  // the job rows share it. DownloadManager renders nothing when both halves are
  // empty, which is the same rule as before applied once instead of twice.
  const lines = new Map<string, string>();
  for (const job of jobs) {
    if (job.id.startsWith(SCHEDULE_JOB_PREFIX) && isRunning(job)) {
      lines.set(job.id.slice(SCHEDULE_JOB_PREFIX.length), jobStatusLine(job) || job.detail);
    }
  }
  // Cancel all is the QUEUE's — the rows the server would withdraw if asked right
  // now, which is `pending` and nothing else. A live turn is not one of them (it
  // has its own ✕, which stops a running process) and neither is a claimed one
  // (the server refuses `sending`), so the button counts only what it can take —
  // and disappears when nothing left in the card is withdrawable. It is not the
  // card's Clear and never becomes it: Clear dismisses rows for work that ENDED.
  const offerCancelAll = showCancelAll(rows);

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

  return (
    <DownloadManager
      queue={{
        ...queueCount(rows),
        rows: rows.map((row) => (
          <Row
            key={row.entry.id}
            row={row}
            jobLine={lines.get(row.entry.id) ?? ""}
            onDone={refresh}
            onNote={setNote}
          />
        )),
        cancelAll: offerCancelAll ? (
          <button
            type="button"
            className="q-all"
            onClick={cancelAll}
            disabled={busy}
            title="Cancel every queued message"
          >
            Cancel all
          </button>
        ) : null,
        // Only ever the answer to a cancel pressed in this card, so it lives with
        // the card rather than as a toast that would outlive the rows it is about.
        note: note ? <div className="q-note">{note}</div> : null,
      }}
    />
  );
}
