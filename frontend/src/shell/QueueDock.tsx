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
  isRunning,
  jobStatusLine,
  SCHEDULE_JOB_PREFIX,
  type Job,
} from "@platform/lib/jobs";
import { navigateUrl } from "@platform/lib/router";
import DownloadManager from "@platform/ui/DownloadManager";
import { cancelOutcome, explorerUrl, firstLine } from "@shell/schedule-lib";
import {
  drawnIds,
  openRows,
  queueCount,
  queueRows,
  roleText,
  rowCancelKind,
  scheduleRunsEnded,
  showCancelAll,
  type QueueRow,
} from "@shell/queue-dock-lib";
import { pokeTasks } from "@shell/tasksPulse";

// Fast enough that a row appears near the moment it comes due, slow enough to be
// a permanent background poll in every shell. The queue moves on the scheduler's
// own tick, so a second of lag costs nothing.
const POLL_MS = 6000;

// The job registry is where a LIVE turn reports its progress, and its line —
// "waiting for permission", "working · 4210 tokens" — is the most useful string
// in this card, so the row joins it onto the entry by id rather than inventing a
// status of its own. That same job is filtered out of the job rows below — by being
// NAMED in the slot's `drawn` list, not by its id looking schedule-shaped — so one
// run occupies one row in one list, and a poll that comes back empty or fails hands
// the run to the job half rather than leaving it with no row anywhere.
//
// THOSE JOBS ARRIVE FROM THE CARD (`onJobs`) rather than from a poll of our own, and
// that is what keeps the one-row rule true at every instant instead of only when two
// timers agree. This half reads its queue every six seconds; the card reads
// /api/jobs about every second. A run that ended was therefore terminal in the card
// and still live here for seconds, and since `jobRows` drops a drawn run whatever its
// state (it has to — a state-dependent rule was exactly the duplicate), those seconds
// would be the outcome row's wait. Sharing the card's snapshot means this half lets
// the run go as soon as the card knows (`openRows`), and it also deletes what used to
// be a second forever-poll of the same endpoint.
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
          // The LAST snapshot stays. An unreadable queue is not an empty one, and
          // blanking these rows on one bad probe would move a live run from the row
          // with its Explorer link to the plainer job row for no reason.
          //
          // It is no longer the difference between a row and NO row — that was the
          // hole: with the first read failing there is no last snapshot, so this
          // half drew nothing while the job half had already dropped the run on the
          // assumption that it had. The slot now says what these rows cover, and an
          // empty list means the job half draws the run itself.
          //
          // Nor does keeping it strand a finished run's outcome behind a stale live
          // row: `openRows` retires that row against the job snapshot, which comes
          // from the card and does not depend on this read ever succeeding again.
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
  return { snap, refresh };
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
  const { snap, refresh } = useQueue();
  // The card's own job snapshot, handed up on every one of its polls — about once a
  // second while anything is live, against this half's six. It is what the status
  // lines are read from AND what decides when this half lets a run go.
  const [jobs, setJobs] = useState<Job[]>([]);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  // The previous snapshot, for the ended-run comparison below. A ref rather than
  // reading `jobs` in the callback: the callback is handed down once, and a state
  // read inside it would compare against whatever render it was created in.
  const prevJobs = useRef<Job[]>([]);
  // The snapshot is also this corner's earliest knowledge that a run ENDED —
  // about a second behind the turn, where the Tasks page's own poll is up to
  // 20s behind and the status it reads can lag the liveness window on top. So a
  // running→terminal flip on a sys:schedule:* job pokes the shared tasks store
  // (which re-reads, or asks the open Tasks page to): "if finished in one,
  // finished in the other" (Akshil, 2026-08-19).
  const onJobs = useCallback((next: Job[]) => {
    if (scheduleRunsEnded(prevJobs.current, next)) pokeTasks();
    prevJobs.current = next;
    setJobs(next);
  }, []);

  // What this half still owns: its rows minus any live one whose run the registry
  // says has ended. Without that the outcome row would wait for the next queue read
  // to drop the entry — up to a full poll, or forever while that read is failing and
  // its last snapshot stands — because the job half drops a drawn run whatever its
  // state. queue-dock-lib `openRows` holds the argument and the three rows it must
  // NOT retire.
  const rows = openRows(queueRows(snap.live, snap.running, snap.queued), jobs);
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
        // Which runs these rows cover, so the job half drops exactly them. Taken
        // from the same array as the rows below — the two cannot disagree — and
        // empty when a failed read left this half with nothing, which is what
        // hands a live run back to the job half instead of losing it.
        drawn: drawnIds(rows),
        // And the way back: the card's job snapshot, which is how this half learns a
        // run ended without waiting for its own slower read (see `rows` above) —
        // and how the Tasks page learns it too (the poke in onJobs above).
        onJobs,
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
