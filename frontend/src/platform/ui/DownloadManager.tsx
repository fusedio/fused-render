// The activity card — ONE card at the foot of the notification stack for every
// piece of work in progress: the long-running operations any page reported (SPEC
// §36, D244) and the scheduled messages about to run or running now.
//
// It exists because that work used to be invisible the moment you looked away
// from it: a page pulling an 8GB model drew its own bar inside itself, and the
// shell tears a page's frame down on every navigation, so browsing to another
// file while the download ran left multi-GB of traffic happening with nothing on
// screen to say so. The record lives on the server (fused_render/jobs.py), which
// is why this survives the reporter's document — and why it can show work
// reported by a page in a different browser tab, or by a detached Python worker
// posting to /api/jobs itself.
//
// IT IS ONE CARD, and that is the recent change (Akshil, 2026-08-17): "this queue
// and notification thing should be same no? why duplicate popups? just replace
// the queue -> thinking -> done". The queue used to draw its own card stacked
// directly above this one — same corner, same plate, same shape, same kind of
// thing — so a scheduled run appeared in the top card while it waited and the
// bottom one once it had finished. One run, changing container mid-life, under two
// headers and two counts. Now there is one container and one lifecycle in it:
//
//     queued → starting → running → finished / failed
//
// The first three of those states are the QUEUE's rows and the last is a job row,
// which is why the queue arrives as a slot (`queue` below) rather than being
// polled here: those rows have to offer "Open in Explorer", whose one answer lives
// in shell/schedule-lib, and platform may not import shell
// (frontend/scripts/check-boundaries.mjs). So the shell renders the rows and this
// card owns everything shared — the plate, the one header, the one count, the one
// scrolling list, the collapse, and Clear.
//
// THE FOLD TAKES THE JOB ROWS ONLY, and that is deliberate (jobs.ts `rowsShown`
// holds the rule and the full argument). The collapse is a persisted preference,
// so it was set against the card as it used to be — a download history worth
// folding away — and once the queue arrived in the same card, folding the list
// took the only cancel a queued message or a live turn has with it. A card
// somebody collapsed weeks ago then left scheduled work arriving with nothing to
// stop it. The queue's rows therefore stay on screen whatever the fold says; the
// job rows are the half that folds; and nothing here rewrites the stored
// preference, because the user set it on purpose.
//
// WHICH HALF OWNS A RUN IS TOLD, NOT GUESSED (`queue.drawn`, jobs.ts `jobRows`).
// One row per unit of work needs the two halves to agree on who is drawing what, and
// this half used to assume: it dropped every running `sys:schedule:*` job because a
// queue row for it probably existed. It does not always exist — the queue read can
// fail, and this card can be mounted bare with nothing filling the slot — and a run
// that was genuinely executing then had no row in either half, so no stop anywhere.
// The slot now carries the entry ids its rows cover; this half drops exactly those
// and draws the rest itself, which for a live run is a row with the same title, the
// same status line and the same ✕, only without the Explorer link that needs shell.
// `foldedJobRows` keeps that stand-in row through the fold, for the same reason the
// queue's own rows go through it.
//
// AND IT IS TOLD ABOUT A RUN, NOT ABOUT A STATE: a drawn run is dropped here whether
// its job is running or finished. The exemption terminal rows used to have was an
// argument about the server (a turn that has ended has left the queue) applied to two
// clocks reading it — this poll runs about every second, the queue's every six — so a
// run that ended had its outcome row here while the other half still painted it live,
// two rows for the one lifecycle, for as long as several seconds. The other side of
// that trade is that the outcome row waits for the queue half to let go, so this card
// hands its snapshot BACK through the slot (`onJobs`) and the queue half retires the
// row against it (queue-dock-lib `openRows`): the handover is a render apart instead
// of a poll apart, and it does not depend on the queue endpoint answering at all.
//
// Placement and stacking still belong to NotificationHost: this component
// positions nothing. It sits ABOVE the server card and BELOW the toasts, because
// those are the three lifetimes in the column — a toast is seconds, work in
// progress is minutes, the server card outlives the session.
//
// Cancel is a REQUEST, not a kill, for a JOB row (jobs.py `request_cancel`): the
// shell has no idea what the work is or which process is doing it, so the ✕ sets
// a flag the reporting page reads on its next tick and acts on. The row therefore
// says "Cancelling…" until the work actually stops, rather than lying about it. A
// queue row's ✕ is a different promise and the shell owns it.
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import {
  cancelJob,
  clearFinishedJobs,
  dismissJob,
  fetchJobs,
  foldedJobRows,
  isRunning,
  jobAmount,
  jobFraction,
  jobRows,
  jobStatusLine,
  jobsSummary,
  overallFraction,
  pollInterval,
  rowsShown,
  JOB_PING_KEY,
  type Job,
  type QueueCount,
} from "@platform/lib/jobs";
import { navigateUrl } from "@platform/lib/router";
import { TroubleCard } from "@platform/ui/TroubleCard";
import { failureContextFromJob, fixSessionUrl, startSelfFix } from "@platform/lib/selffix";

const COLLAPSED_KEY = "fused-render:jobs-collapsed";

function loadCollapsed(): boolean {
  try {
    return localStorage.getItem(COLLAPSED_KEY) === "1";
  } catch {
    return false; // private mode / disabled storage — expanded is the honest default
  }
}

function saveCollapsed(collapsed: boolean): void {
  try {
    localStorage.setItem(COLLAPSED_KEY, collapsed ? "1" : "0");
  } catch {
    /* best-effort, like every other persisted chrome flag */
  }
}

// Poll /api/jobs, adapting the cadence to whether anything is live, and poll
// IMMEDIATELY when another same-origin document says it just reported.
//
// The ping is what makes a download appear the instant it starts instead of up
// to POLL_IDLE_MS later. It is only an optimisation: a reporter with no JS (a
// Python worker) writes no ping, so the idle poll below is the floor that
// guarantees the row shows up either way.
function useJobs(): {
  jobs: Job[];
  refresh: () => void;
  patch: (fn: (jobs: Job[]) => Job[]) => void;
} {
  const [jobs, setJobs] = useState<Job[]>([]);
  // Read by the scheduler without re-arming it: the poll loop re-reads the
  // cadence after every response, so `jobs` must not be in its dependency list
  // or every tick would tear the timer down and build a new one.
  const jobsRef = useRef<Job[]>(jobs);
  jobsRef.current = jobs;
  const pollRef = useRef<() => void>(() => {});
  // Bumped by every mutation — a request the user made, or a read this hook
  // asked for after one. A response issued BEFORE that describes the list as it
  // was, so painting it flicks the row the user just dismissed back onto the
  // screen. Lives in a ref rather than the effect closure because `patch`
  // (outside the effect) has to invalidate an in-flight read as well.
  const epochRef = useRef(0);

  useEffect(() => {
    let disposed = false;
    let timer: number | undefined;
    let inFlight = false;
    // A read asked for while one was already in flight. Without this the
    // request is simply dropped — and the request that gets dropped is almost
    // always the one that matters, because every mutation (cancel / dismiss /
    // clear) asks for a read the moment it lands.
    let queued = false;

    const schedule = (ms: number) => {
      window.clearTimeout(timer);
      timer = window.setTimeout(poll, ms);
    };

    async function poll() {
      // A page hidden behind another tab is not being read; its throttled
      // timers would fire in a clump on return anyway. Keep the loop alive at
      // the idle cadence so the first visible frame is fresh.
      if (document.visibilityState === "hidden") {
        schedule(pollInterval(jobsRef.current));
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
        if (at === epochRef.current) {
          setJobs(snapshot.jobs);
          schedule(pollInterval(snapshot.jobs));
        } else {
          // Stale. Dropped rather than painted; `queued` is set (the mutation
          // asked for a read while this one was in flight), so the fresh read
          // is already on its way.
          schedule(pollInterval(jobsRef.current));
        }
      } catch {
        // The server being unreachable is the ServerStatusBanner's story to
        // tell, not this card's — keep the last list on screen and retry at the
        // idle cadence rather than blanking the manager on one failed probe.
        if (!disposed) schedule(pollInterval(jobsRef.current));
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

  // Apply a change the SERVER has already confirmed, without waiting for a read
  // to tell us what we just did. A dismiss is a request that answered 200 — the
  // row is gone — so leaving it on screen until the next poll lands makes the ✕
  // feel broken, and on the idle cadence that wait is seconds. The poll still
  // reconciles; this only removes the gap.
  const patch = useCallback((fn: (jobs: Job[]) => Job[]) => {
    epochRef.current += 1; // any read already in flight predates this
    setJobs(fn);
  }, []);

  return { jobs, refresh, patch };
}

function Bar({ job }: { job: Job }) {
  const fraction = jobFraction(job);
  const tone =
    job.state === "error"
      ? " is-error"
      : job.state === "done"
        ? " is-done"
        : job.stalled
          ? " is-stalled"
          : "";
  // No fraction to draw and still running = indeterminate: a narrow fill that
  // travels, rather than a width that grows. The alternative — parking a real
  // bar at some invented percentage — is what makes a live download read as
  // frozen (the same lesson as the install loader's D213 sweep).
  const indeterminate = fraction === null && isRunning(job) && !job.stalled;
  // Nothing to say: a job that ended (or stalled) without ever reporting a
  // total has no progress to draw, and an empty track under an error message is
  // decoration that reads as "0% done" — which is not what happened.
  if (fraction === null && !indeterminate) return null;
  return (
    <div className={"dl-bar" + tone}>
      <div
        className={"dl-bar-fill" + (indeterminate ? " is-indeterminate" : "")}
        // `data-indeterminate` is the DOM-observable contract (the install
        // loader's convention): no headless test can see whether an animation
        // LOOKS right, but it can see which mode the bar is in.
        data-indeterminate={indeterminate ? "1" : undefined}
        style={indeterminate ? undefined : { width: `${(fraction as number) * 100}%` }}
      />
    </div>
  );
}

// "Fix this" — the self-fix trigger (SPEC §43, SF-1). Offered on a FAILED row
// and nowhere else, which is the whole of its placement argument: a failure is
// the one moment where the app has already admitted it cannot do the thing, so
// an offer to go and look at why is not an interruption. On a running row it
// would be noise; on a finished one it would be a question nobody asked.
//
// Starting the session is only half the click. The other half is LANDING THE
// USER IN IT: the shell navigates to the install folder with the chat sidebar
// attached to the run that was just started, so what happens next is something
// they watch and answer permission cards for, not something that happens to
// their app while they look at a spinner.
function FixButton({ job, onError }: { job: Job; onError: (msg: string | null) => void }) {
  const [busy, setBusy] = useState(false);

  const start = async () => {
    setBusy(true);
    onError(null);
    try {
      const started = await startSelfFix(failureContextFromJob(job));
      // A directory, always — the install root. See SelfFixPanel for why the
      // hint matters on this particular navigation.
      navigateUrl(fixSessionUrl(started), { isDir: true });
    } catch (e) {
      // Reported ON THE ROW (the caller renders it under the status line)
      // rather than as a toast: the row is what the user clicked, and the most
      // likely refusal — "this installation is read-only" — is a fact about the
      // app that they need in front of the thing that failed.
      onError(String((e as Error)?.message || e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <button
      className="dl-fix"
      onClick={start}
      disabled={busy}
      title="Open a Claude session on this installation and try to fix it here"
    >
      {busy ? "Starting…" : "Fix this"}
    </button>
  );
}

function JobRow({
  job,
  onChanged,
  onPatch,
}: {
  job: Job;
  onChanged: () => void;
  onPatch: (fn: (jobs: Job[]) => Job[]) => void;
}) {
  const [busy, setBusy] = useState(false);
  // Why the self-fix start's failure lives on the ROW rather than in the button
  // that raised it: see FixButton.
  const [fixError, setFixError] = useState<string | null>(null);
  const running = isRunning(job);
  const fraction = jobFraction(job);
  const amount = jobAmount(job);
  const status = jobStatusLine(job);

  // One ✕ with two meanings, because they are the same intent at two points in
  // a job's life: stop this / take it off my screen. A running job that its
  // reporter never marked cancellable has no ✕ at all rather than a dead one —
  // the flag would be set and nothing would ever read it.
  //
  // A STALLED row dismisses rather than cancels: there is nobody left to hear a
  // cancel request, and the row is the app admitting it has stopped knowing —
  // so letting the user close it hides nothing the app could otherwise say.
  const canCancel = running && job.cancellable && !job.cancel_requested && !job.stalled;
  const canDismiss = !running || job.stalled;

  const act = async () => {
    setBusy(true);
    try {
      if (canCancel) {
        await cancelJob(job.id);
        // The row stays — the work has not stopped — but the label has to move
        // to "Cancelling…" now, or the ✕ reads as having done nothing.
        onPatch((js) => js.map((j) => (j.id === job.id ? { ...j, cancel_requested: true } : j)));
      } else {
        await dismissJob(job.id);
        onPatch((js) => js.filter((j) => j.id !== job.id));
      }
    } catch {
      /* nothing applied locally — the refresh below is the source of truth */
    } finally {
      setBusy(false);
      onChanged();
    }
  };

  return (
    <div className={"dl-row" + (job.stalled ? " is-stalled" : "")}>
      <div className="dl-row-head">
        <span className="dl-title" title={job.page || undefined}>
          {job.title}
        </span>
        {amount && <span className="dl-amount">{amount}</span>}
        {fraction !== null && running && (
          <span className="dl-pct">{Math.round(fraction * 100)}%</span>
        )}
        {job.state === "error" && <FixButton job={job} onError={setFixError} />}
        {(canCancel || canDismiss) && (
          <button
            className="dl-x"
            onClick={act}
            disabled={busy}
            title={canCancel ? "Cancel" : "Dismiss"}
            aria-label={canCancel ? `Cancel ${job.title}` : `Dismiss ${job.title}`}
          >
            ✕
          </button>
        )}
      </div>
      <Bar job={job} />
      {status && <div className="dl-status">{status}</div>}
      {/* The compact trouble card rather than a bare red line: the most likely
          failure here is Claude Code missing or signed out, and "Claude didn't
          start" with nowhere to go is the shape of message this whole surface
          exists to replace. Facts are not fetched for this one — the card sits
          in a notification and the Preferences tab is where the full report
          lives. */}
      {fixError && (
        <TroubleCard
          compact
          what={`starting a fix session for "${job.title || job.id}"`}
          error={fixError}
        />
      )}
    </div>
  );
}

/**
 * The queue half of this card, handed in by the shell.
 *
 * Data and nodes, not a component: the count has to reach the ONE header and the
 * rows have to land in the ONE list, and the rows themselves can only be built in
 * shell (they speak `explorerUrl`). So the shell renders exactly the parts that
 * are its own — the rows, the Cancel all button, the sentence answering it — and
 * hands over the numbers this card needs to describe them.
 */
export interface QueueSlot extends QueueCount {
  /** The queue's rows, already rendered, in lifecycle order (running first, then
   *  starting, then waiting). Each is a `.q-row`, a sibling of the `.dl-row`s. */
  rows: ReactNode;
  /** Which scheduled runs those rows cover, by entry id (queue-dock-lib
   *  `drawnIds`) — so the job half can drop exactly them and nothing else.
   *
   *  Rendered nodes are opaque, so the ids travel beside them rather than being
   *  read back out of them; they come off the same array, so the two cannot
   *  disagree. An EMPTY list is meaningful and not a bug: it says this half is
   *  drawing nothing (a failed queue read keeps its last snapshot, which after a
   *  failed first read is empty), and the job half then draws the run itself
   *  instead of assuming somebody else has. See `jobRows`. */
  drawn: string[];
  /** This card's job snapshot, handed BACK to the queue half on every poll.
   *
   *  The ids above only work if the two halves are looking at the same run at the
   *  same moment, and they were not: this card polls /api/jobs about once a second
   *  and the queue half polls its own endpoint every six, so a run that ended was
   *  terminal here while it was still live there — one run, two rows, for seconds.
   *  `jobRows` now drops a drawn run whatever its state, which makes the duplicate
   *  impossible and leaves the outcome row waiting on the queue half to let go; this
   *  callback is what makes it let go promptly, by giving it the very snapshot that
   *  says the turn is over (queue-dock-lib `openRows`) instead of its own read six
   *  seconds later — or never, if that read is failing and its last snapshot stands.
   *
   *  It is the FULL list, not the filtered one: what the queue half needs is the
   *  registry as it stands, including the runs this card is not drawing because that
   *  half is. It also spares the queue half a second forever-poll of the same
   *  endpoint, which is what it did before. */
  onJobs?: (jobs: Job[]) => void;
  /** Cancel all, when the shell has 2+ genuinely withdrawable rows. */
  cancelAll?: ReactNode;
  /** What a cancel actually did, including the half that was refused. */
  note?: ReactNode;
}

export default function DownloadManager({ queue }: { queue?: QueueSlot }) {
  const { jobs: reported, refresh, patch } = useJobs();
  const [collapsed, setCollapsed] = useState(loadCollapsed);
  // Hand this poll's snapshot back to the queue half, so the run it is drawing is
  // retired against the same evidence this half is acting on rather than against a
  // read six seconds behind it (`QueueSlot.onJobs`, queue-dock-lib `openRows`). In an
  // effect and not in the render body: it sets state in the parent, and doing that
  // while rendering is what React warns about. Keyed on the array identity, which
  // changes exactly once per response or per local patch.
  const onJobs = queue?.onJobs;
  useEffect(() => {
    onJobs?.(reported);
  }, [onJobs, reported]);
  // Everything the poll returned MINUS the scheduled runs the queue's own rows are
  // actually drawing — told, never assumed (`queue.drawn`), so a queue read that
  // failed and a card mounted with no queue at all both leave the run one row here
  // rather than none anywhere. jobs.ts `jobRows` owns the argument.
  // `patch`/`refresh` still work on the full list — the filter is what this card
  // SHOWS, not what it knows.
  const jobs = jobRows(reported, queue?.drawn);
  const count: QueueCount = { waiting: queue?.waiting ?? 0, running: queue?.running ?? 0 };
  const queued = count.waiting + count.running;

  // Nothing to say — render nothing at all, no chrome. The card is a picture of
  // what is happening now, so an empty one is not an empty state with a header
  // reading "nothing queued", it is no card. Both halves have to be empty: a
  // queue row with no jobs is still work in progress worth a card.
  if (jobs.length === 0 && queued === 0) return null;

  const overall = overallFraction(jobs);
  // WHAT THE FOLD TAKES, and it is not the whole list — jobs.ts `rowsShown` owns
  // the rule and says why. Short version: the collapse is a persisted preference
  // set against a growing download history, and once the queue moved into this
  // card, folding the list took the only cancel a queued message or a live turn
  // has with it. So the queue's rows stay whatever the fold says, and the job rows
  // are the half that folds.
  const shown = rowsShown(collapsed, count);
  // The job rows this card is DRAWING: all of them open, and folded only the ones
  // the fold must not take — a live scheduled run standing in for a queue row that
  // is not there (`foldedJobRows`). Nothing the preference was set for survives it.
  const listed = shown.jobs ? jobs : foldedJobRows(jobs);
  // What "Clear" would actually take — which includes stalled rows, since those
  // are dismissible too. Counting only finished ones hid the button in exactly
  // the case a user most wants it: a column of rows nobody is reporting on.
  const clearable = jobs.filter((j) => !isRunning(j) || j.stalled).length;

  const toggle = () => {
    setCollapsed((was) => {
      saveCollapsed(!was);
      return !was;
    });
  };

  const clear = async () => {
    try {
      await clearFinishedJobs();
      // Mirrors the server's rule (jobs.py `clear_finished`): everything that
      // is not still being reported on.
      patch((js) => js.filter((j) => isRunning(j) && !j.stalled));
    } catch {
      /* nothing applied locally — the refresh below is the source of truth */
    }
    refresh();
  };

  return (
    <div className="dl-host">
      <div className="dl-head">
        <button
          className="dl-toggle"
          onClick={toggle}
          aria-expanded={!collapsed}
          title={collapsed ? "Show details" : "Hide details"}
        >
          <span className={"dl-chevron" + (collapsed ? " is-collapsed" : "")} aria-hidden="true">
            ⌄
          </span>
          <span className="dl-summary">{jobsSummary(jobs, count)}</span>
        </button>
        {overall !== null && <span className="dl-pct">{Math.round(overall * 100)}%</span>}
        {/* Two actions, and they are not the same one twice: Cancel all withdraws
            messages that have not gone yet (the shell's, and only when 2+ rows
            genuinely can be), Clear dismisses rows for work that has ENDED. So a
            terminal row is clearable without a live one being touched. */}
        {queue?.cancelAll}
        {clearable > 0 && (
          <button className="dl-clear" onClick={clear} title="Dismiss finished">
            Clear
          </button>
        )}
      </div>
      {/* Collapsed still shows the overall bar: folding the job rows away should
          hide the detail, not the fact that something is running. With nothing
          running there is no bar — a sweep under a header reading "2 finished"
          would animate work that is over. */}
      {collapsed && jobs.some(isRunning) && (
        <div className="dl-bar">
          <div
            className={"dl-bar-fill" + (overall === null ? " is-indeterminate" : "")}
            data-indeterminate={overall === null ? "1" : undefined}
            style={overall === null ? undefined : { width: `${overall * 100}%` }}
          />
        </div>
      )}
      {/* ONE list, in lifecycle order: the queue's rows first (running, then
          starting, then waiting) and the job rows under them, which is where the
          same run lands once its turn has ended. A scheduled message therefore
          moves down this list rather than jumping between two cards.
          Folded, the same list holds the queue's rows alone — `is-folded` caps it
          shorter, so the fold still buys a small card even with a dozen entries
          past due after a wake. Cancel all keeps its 2+ threshold precisely
          because of this: for a single row the row's own ✕ is reachable either
          way, and it is the same action with a better name on it. */}
      {(shown.queue || listed.length > 0) && (
        <div className={"dl-rows" + (shown.jobs ? "" : " is-folded")}>
          {queue?.rows}
          {listed.map((job) => (
            <JobRow key={job.id} job={job} onChanged={refresh} onPatch={patch} />
          ))}
        </div>
      )}
      {/* Below the rows and OUTSIDE the collapse: it answers Cancel all, which is
          in the header and pressable while the list is folded away. */}
      {queue?.note}
    </div>
  );
}
