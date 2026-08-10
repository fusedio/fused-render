// The download manager — one card at the foot of the notification stack showing
// every long-running operation any page reported (SPEC §36, D244).
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
// Placement and stacking belong to NotificationHost, exactly like the server
// card below it: this component positions nothing. It sits ABOVE that card and
// BELOW the toasts, because those are the three lifetimes in the column — a
// toast is seconds, a job is minutes, the server card outlives the session.
//
// Cancel is a REQUEST, not a kill (jobs.py `request_cancel`): the shell has no
// idea what the work is or which process is doing it, so the ✕ sets a flag the
// reporting page reads on its next tick and acts on. The row therefore says
// "Cancelling…" until the work actually stops, rather than lying about it.
import { useCallback, useEffect, useRef, useState } from "react";
import {
  cancelJob,
  clearFinishedJobs,
  dismissJob,
  fetchJobs,
  isRunning,
  jobAmount,
  jobFraction,
  jobStatusLine,
  jobsSummary,
  overallFraction,
  pollInterval,
  JOB_PING_KEY,
  type Job,
} from "@platform/lib/jobs";

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
function useJobs(): { jobs: Job[]; refresh: () => void } {
  const [jobs, setJobs] = useState<Job[]>([]);
  // Read by the scheduler without re-arming it: the poll loop re-reads the
  // cadence after every response, so `jobs` must not be in its dependency list
  // or every tick would tear the timer down and build a new one.
  const jobsRef = useRef<Job[]>(jobs);
  jobsRef.current = jobs;
  const pollRef = useRef<() => void>(() => {});

  useEffect(() => {
    let disposed = false;
    let timer: number | undefined;
    let inFlight = false;

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
      if (inFlight) return;
      inFlight = true;
      try {
        const snapshot = await fetchJobs();
        if (disposed) return;
        setJobs(snapshot.jobs);
        schedule(pollInterval(snapshot.jobs));
      } catch {
        // The server being unreachable is the ServerStatusBanner's story to
        // tell, not this card's — keep the last list on screen and retry at the
        // idle cadence rather than blanking the manager on one failed probe.
        if (!disposed) schedule(pollInterval(jobsRef.current));
      } finally {
        inFlight = false;
      }
    }

    pollRef.current = () => {
      if (!disposed) poll();
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
  return { jobs, refresh };
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

function JobRow({ job, onChanged }: { job: Job; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
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
      if (canCancel) await cancelJob(job.id);
      else await dismissJob(job.id);
    } catch {
      /* the row is about to be re-read from the server either way */
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
    </div>
  );
}

export default function DownloadManager() {
  const { jobs, refresh } = useJobs();
  const [collapsed, setCollapsed] = useState(loadCollapsed);

  // Nothing to say — render nothing at all. The manager is a picture of what is
  // happening now, so an empty one is not an empty card, it is no card.
  if (jobs.length === 0) return null;

  const overall = overallFraction(jobs);
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
    } catch {
      /* re-read below regardless */
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
          <span className="dl-summary">{jobsSummary(jobs)}</span>
        </button>
        {overall !== null && <span className="dl-pct">{Math.round(overall * 100)}%</span>}
        {clearable > 0 && (
          <button className="dl-clear" onClick={clear} title="Dismiss finished">
            Clear
          </button>
        )}
      </div>
      {/* Collapsed still shows the overall bar: folding the rows away should
          hide the detail, not the fact that something is running. With nothing
          running there is no bar — a sweep under a header reading "2 finished"
          would animate work that is over. */}
      {collapsed ? (
        jobs.some(isRunning) && (
          <div className="dl-bar">
            <div
              className={"dl-bar-fill" + (overall === null ? " is-indeterminate" : "")}
              data-indeterminate={overall === null ? "1" : undefined}
              style={overall === null ? undefined : { width: `${overall * 100}%` }}
            />
          </div>
        )
      ) : (
        <div className="dl-rows">
          {jobs.map((job) => (
            <JobRow key={job.id} job={job} onChanged={refresh} />
          ))}
        </div>
      )}
    </div>
  );
}
