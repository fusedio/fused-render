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
// EVERYTHING FOLDS NOW (D562, user call 2026-08-27, reversing D558/D559's
// exemptions): collapsing hides the queue's rows exactly like the job rows,
// with no kind of row pinned outside it. That used to be deliberately
// asymmetric — the queue's rows and a live scheduled run's stand-in job row
// were exempt, because folding away a queued message's or a live turn's only
// ✕ left a card collapsed weeks ago with scheduled work arriving and nothing
// on screen to stop it. The user's rule is that there is no such thing as a
// "non-foldable card", full stop.
//
// COLLAPSED IS NOW A CHIP, NOT A SHORT CARD (D563, status bar redesign, user
// call: "the collapsed notification is also taking too much space... it is
// impossible to use the claude template with it"). `.dl-toggle` — the
// category name, its count (D573), the aggregate percentage — is the WHOLE
// of what renders while collapsed; `queue?.cancelAll` and `queue?.note`, along with
// Clear and every row, render only inside the panel that opens when the
// card is expanded, and so no longer survive a collapse the way D562's own
// paragraph here used to promise. That reachability requirement is gone on
// purpose, not merely forgotten: the chip's whole point is to cost the page
// nothing but one summary line, and a button on it would be a second thing
// competing for that line's very little room. Nothing here rewrites the
// stored preference; it is only ever changed by the user pressing the chip.
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
// This stand-in row folds like any other now (D562) — collapsed, it is not on
// screen at all any more (D563's chip carries no row, and no button either).
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
// PLACEMENT MOVED (D563): this card used to sit in NotificationHost's fixed,
// floating column with the toasts and the server card, ABOVE the server card
// and BELOW the toasts by lifetime (a toast is seconds, work in progress is
// minutes, the server card outlives the session) — but a FIXED column
// overlays page content even collapsed, which is what made a page like the
// Claude template unusable under it. It is handed to `StatusBar` now instead,
// mounted inside `#main` where it RESERVES layout space rather than floating
// over it; the toasts, `FdaCard` and `ServerStatusBanner` are unaffected and
// stay in NotificationHost's column, since none of them are long-lived
// enough to be worth a permanently reserved strip. This component still
// positions nothing itself — `StatusBar` owns the chip's place in the bar,
// and this file's own CSS classes own the panel floating above it.
//
// Cancel is a REQUEST, not a kill, for a JOB row (jobs.py `request_cancel`): the
// shell has no idea what the work is or which process is doing it, so the ✕ sets
// a flag the reporting page reads on its next tick and acts on. The row therefore
// says "Cancelling…" until the work actually stops, rather than lying about it. A
// queue row's ✕ is a different promise and the shell owns it.
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useAutoExpandOnNew } from "@platform/lib/autoExpand";
import StatusDot from "@platform/ui/StatusDot";
import { useExclusiveSection } from "@platform/lib/exclusiveSection";
import { useDismissOnOutside } from "@platform/lib/dismissOnOutside";
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
  jobsAfterClear,
  jobStatusLine,
  pollInterval,
  JOB_PING_KEY,
  SCHEDULE_JOB_PREFIX,
  type Job,
  type QueueCount,
} from "@platform/lib/jobs";
import { repoName } from "@platform/lib/format";
const COLLAPSED_KEY = "fused-render:jobs-collapsed";

function loadCollapsed(): boolean {
  try {
    // `!== "0"`, NOT `=== "1"` (D595): an ABSENT key means COLLAPSED, which is
    // every section's state on a fresh profile and was the bug — four panels
    // opened over the page at once, and the D582 arbiter then picked which one
    // survived by registration order rather than by anything meaningful. The
    // chip's circle already says whether there is anything inside, so an
    // auto-opened EMPTY panel communicates nothing and covers the page to do
    // it; "expanded is the honest default" was written when the chip carried a
    // count and the panel was the only way to see detail.
    //
    // THE STORED VALUES KEEP THEIR MEANINGS — no sentinel flip, so no
    // migration: `"1"` is still collapsed, and `"0"` is still expanded, so
    // someone who deliberately opened this section stays opened. Only the
    // absent case moves.
    return localStorage.getItem(COLLAPSED_KEY) !== "0";
  } catch {
    // Collapsed here too: a private-mode profile takes this branch on EVERY
    // load, so it is the one case that never gets to express a preference.
    return true;
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
  /** Has /api/jobs answered once? `jobs` starts `[]` and stays `[]` on an idle
   *  machine, so the list cannot tell "not asked yet" from "genuinely
   *  nothing" — the distinction `useAutoExpandOnNew` needs to avoid reading
   *  pre-existing jobs as arrivals on load (D574 bug 2). */
  settled: boolean;
  jobs: Job[];
  refresh: () => void;
  patch: (fn: (jobs: Job[]) => Job[]) => void;
} {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [settled, setSettled] = useState(false);
  // Read by the scheduler without re-arming it: the poll loop re-reads the
  // cadence after every response, so `jobs` must not be in its dependency list
  // or every tick would tear the timer down and build a new one.
  const jobsRef = useRef<Job[]>(jobs);
  jobsRef.current = jobs;
  const pollRef = useRef<() => void>(() => {});
  // When a running job was last seen, so the poll loop can hold the ACTIVE
  // cadence for GRACE_MS after the last one disappears (see jobs.ts). Starts
  // at -Infinity: on first mount nothing has been seen running yet, so there
  // is no grace to extend.
  const lastRunningAtRef = useRef<number>(-Infinity);
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

    // Records `jobs` as the latest known snapshot and schedules the next poll
    // off it — updating `lastRunningAtRef` first, so a job that just stopped
    // running still gets the grace window rather than an immediate idle drop.
    const scheduleFor = (jobs: Job[]) => {
      const now = Date.now();
      if (jobs.some(isRunning)) lastRunningAtRef.current = now;
      schedule(pollInterval(jobs, now - lastRunningAtRef.current));
    };

    async function poll() {
      // A page hidden behind another tab is not being read; its throttled
      // timers would fire in a clump on return anyway. Keep the loop alive at
      // the idle cadence so the first visible frame is fresh.
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
          // Stale. Dropped rather than painted; `queued` is set (the mutation
          // asked for a read while this one was in flight), so the fresh read
          // is already on its way.
          scheduleFor(jobsRef.current);
        }
      } catch {
        // The server being unreachable is the ServerStatusBanner's story to
        // tell, not this card's — keep the last list on screen and retry at the
        // idle cadence rather than blanking the manager on one failed probe.
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

  // Apply a change the SERVER has already confirmed, without waiting for a read
  // to tell us what we just did. A dismiss is a request that answered 200 — the
  // row is gone — so leaving it on screen until the next poll lands makes the ✕
  // feel broken, and on the idle cadence that wait is seconds. The poll still
  // reconciles; this only removes the gap.
  const patch = useCallback((fn: (jobs: Job[]) => Job[]) => {
    epochRef.current += 1; // any read already in flight predates this
    setJobs(fn);
  }, []);

  return { jobs, settled, refresh, patch };
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

// Exported for jobrow.test.tsx only — every other caller goes through
// DownloadManager itself. react-test-renderer (the hook-harness.ts pattern)
// is the only thing in this suite that can render a component with no DOM,
// and it needs the component, not just the pure functions it's built on.
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
  /** Test seam only (D572's own failure-path test uses it) — every real
   *  caller gets the real `cancelJob`/`dismissJob` (@platform/lib/jobs) by
   *  default. Injectable rather than mocked so a rejected-request test does
   *  not need a process-wide `mock.module` on `@platform/lib/jobs` (this
   *  file's own header comment already tells that contamination story for
   *  `@platform/lib/api`, which this module itself calls into). */
  cancelFn?: (id: string) => Promise<Job>;
  dismissFn?: (id: string) => Promise<{ dismissed: string }>;
}) {
  const [busy, setBusy] = useState(false);
  // A REJECTED cancel/dismiss must say so, not vanish (D572, user: "the
  // cancel button also doesn't seem to be doing anything?" — a 404, a 500 or
  // an offline server all left the empty `catch` below discarding the
  // failure with no toast, no console entry, no state change and no label
  // move, so the click produced literally nothing observable). Same posture
  // D566 already set for this exact situation in the Models panel's own
  // Unload ("must not fail silently") — a row-scoped `.dl-status` line, not
  // a toast, since the row is already the thing the user is looking at.
  // `onPatch`/`onChanged()`'s "the refresh is the source of truth" framing
  // (below) is still correct for the SUCCESS path and for an ordinary race —
  // the server is authoritative about whether the work actually stopped —
  // it was only ever wrong for a request that never landed at all.
  const [failure, setFailure] = useState<string | null>(null);
  const running = isRunning(job);
  const fraction = jobFraction(job);
  const amount = jobAmount(job);
  const status = jobStatusLine(job);

  // Two controls, one meaning each — because "stop this" and "take it off my
  // screen" read as the same gesture when both hide behind an identical ✕, and
  // that is exactly the confusion users hit: sometimes the cross cancels,
  // sometimes it dismisses, with nothing but a tooltip to tell them apart. A
  // running, cancellable row now gets a text `Cancel`; the ✕ glyph is reserved
  // for dismiss and means only that, everywhere in the notification stack. A
  // running job that its reporter never marked cancellable has no control at
  // all rather than a dead one — the flag would be set and nothing would ever
  // read it.
  //
  // A STALLED row dismisses rather than cancels: there is nobody left to hear a
  // cancel request, and the row is the app admitting it has stopped knowing —
  // so letting the user close it hides nothing the app could otherwise say.
  const canCancel = running && job.cancellable && !job.cancel_requested && !job.stalled;
  const canDismiss = !running || job.stalled;

  const cancel = async () => {
    setBusy(true);
    setFailure(null);
    try {
      await cancelFn(job.id);
      // The row stays — the work has not stopped — but the label has to move
      // to "Cancelling…" now, or the button reads as having done nothing.
      onPatch((js) => js.map((j) => (j.id === job.id ? { ...j, cancel_requested: true } : j)));
    } catch {
      // The request never landed — a 404 against a job id the server never
      // heard of, a 500, an offline server. `onPatch` above did NOT run, so
      // there is nothing for the refresh to correct; without this the button
      // just goes quiet, which reads as broken because it is.
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
      // Same class of problem as `cancel` above: a rejected request left the
      // row exactly as it was with nothing said about why.
      setFailure("Could not dismiss — check your connection and retry.");
    } finally {
      setBusy(false);
      onChanged();
    }
  };

  // Belt-and-braces, not the mechanism: `DownloadManager`'s `isVanishedOnSuccess`
  // is what actually keeps a vanished job out of the header count, the rows and
  // the empty-card gate — this is a cheap second guard for any caller that
  // renders a `JobRow` directly (this file's own test does). Schedule-aware
  // for the same reason that filter is: a scheduled run's own outcome row
  // (`sys:schedule:*`) is DELIBERATELY drawn as a real row rather than vanishing
  // on success, so this must not blanket-hide every "done" job.
  if (job.state === "done" && !job.id.startsWith(SCHEDULE_JOB_PREFIX)) return null;

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
        {canCancel && (
          <button
            className="dl-row-cancel"
            onClick={cancel}
            disabled={busy}
            title="Cancel"
            aria-label={`Cancel ${job.title}`}
          >
            Cancel
          </button>
        )}
        {canDismiss && (
          <button
            className="dl-x"
            onClick={dismiss}
            disabled={busy}
            title="Dismiss"
            aria-label={`Dismiss ${job.title}`}
          >
            ✕
          </button>
        )}
      </div>
      {/* THE MODEL, ON ITS OWN LINE (D596, user: "we have a ton of free space in
          the jobs card. why are we truncating stuff instead of placing things
          elsewhere?"). It used to be a suffix on the head line, competing with
          the title for one line's width under D571's shrink ladder — which is
          how a running FLUX row rendered `update picture to be ghibli st…` then
          a lone `F…`: a field minced to one character plus an ellipsis, which
          conveys nothing while still costing width. `jobs.ts`'s own comment
          already calls this a redundant restatement whenever the title names
          the model, so it is the field that should be RELEGATED rather than the
          one that should be minced. Off the head line it gets the panel's full
          width and needs no shrink factor at all.
          Suppressed when it just repeats the title (`_start_resident`/`load`
          set both `title` and `model` to the same model id) — otherwise a
          model-load row would draw the model name twice. The MODEL name only,
          not the whole `owner/model` repo id: the owner is identical for every
          row a given model ever draws. Full id stays on hover, since shortening
          makes two owners' same-named models identical. */}
      {job.model && job.model !== job.title && (
        <div className="dl-model" title={job.model}>
          {repoName(job.model)}
        </div>
      )}
      <Bar job={job} />
      {/* A local action's own failure takes this line over the job's
          ordinary status sentence — it is more urgent and it is about the
          very button the user just pressed. `status` (the server's report)
          comes back once a later poll succeeds or the row's own next action
          clears `failure`. */}
      {(failure ?? status) && <div className="dl-status">{failure ?? status}</div>}
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
  /** "Cancel queued" — a pre-decided node now, not a function of `collapsed`
   *  (D563, status bar redesign: this only ever renders inside the expanded
   *  panel, since the collapsed chip carries no controls at all, so there is
   *  no longer a folded-but-reachable state for a threshold to answer). 2+
   *  genuinely withdrawable rows, since a single row's own cancel control is
   *  the same action with a better name on it and is right there on screen.
   *  `showCancelAll` (queue-dock-lib.ts) owns the actual number — it used to
   *  take `collapsed` too (D562) and drop the threshold to one row for a
   *  card that stayed on screen folded; that call site is gone along with
   *  the promise it was answering. */
  cancelAll?: ReactNode;
  /** What a cancel actually did, including the half that was refused. Like
   *  `cancelAll`, only ever shown inside the expanded panel now (D563) — a
   *  refusal that happened while the card was open stays reachable until the
   *  user re-collapses or presses again, same as before; it just no longer
   *  survives a collapse it didn't cause. */
  note?: ReactNode;
}

// A successful job vanishes from this card entirely (PR #785 follow-up) —
// EXCEPT a scheduled run's own outcome row (`sys:schedule:*`), which
// deliberately survives as a real row until `jobs.py`'s own retention sweeps
// it (Akshil, 2026-08-21: "a run appears, works, and vanishes mid-sentence"
// is the bug this reversal exists to prevent — a collapsed card used to show
// the run thinking and then simply lose the row at the verdict, with no
// surface ever saying it had finished). Everything else that
// reaches `state: "done"` — an image/video/transcription render, a model
// load, a benchmark row — has nothing to say once it has succeeded, so it
// must not draw at all: not a row, not a header count, not a Clear button
// for a row nobody can see. This is presentation-only — jobs.py's own
// FINISHED_TTL_S (3s) still clears the underlying record shortly after the
// first read; this just stops the card from showing it in the meantime.
//
// This is a component-local filter, not a jobs.ts export: other consumers of
// the same registry (apps/ai_models/lib/useCacheScan.ts's title->job map, the
// playground's own stage watchers) need a "done" record to keep existing —
// only this card's presentation of it is what changes here.
function isVanishedOnSuccess(job: Job): boolean {
  return job.state === "done" && !job.id.startsWith(SCHEDULE_JOB_PREFIX);
}

// Exported for `DownloadManager.test.tsx` only, exactly like `JobRow` above —
// every other caller goes through the default export. Pure props in, a tree
// out: no polling, no network, no `window`/`document` — which is what makes
// the parent's own decisions (the empty-card gate, the header count, Clear's
// count, the fold) testable by rendering it directly with a fixed job list,
// rather than by mocking `@platform/lib/api` underneath the real polling
// hook (fragile: that module is shared by dozens of other test files, and a
// global `mock.module` on it does not scope to one file).
export function DownloadManagerView({
  reported,
  ready,
  initialCollapsed,
  queue,
  refresh,
  patch,
}: {
  reported: Job[];
  /** TEST SEAM ONLY — the fold's initial value, defaulting to the persisted
   *  preference (`loadCollapsed`, which since D595 treats an absent key as
   *  COLLAPSED). Every real caller omits it. Injectable rather than mocked for
   *  the reason this file already documents for `cancelFn`/`dismissFn`: a
   *  `mock.module` (or a `globalThis.localStorage` stub) replaces things for
   *  the WHOLE bun process, not one file, and both have contaminated unrelated
   *  suites here before. A test that needs the panel open should SAY so rather
   *  than depend on whatever the fresh-profile default happens to be — which
   *  is exactly what changed under them in D595. */
  initialCollapsed?: boolean;
  /** Has the first /api/jobs read landed (autoExpand.ts's `ready`)? Optional
   *  so a test mounting this view with a fixed list keeps the old behaviour. */
  ready?: boolean;
  queue?: QueueSlot;
  refresh: () => void;
  patch: (fn: (jobs: Job[]) => Job[]) => void;
}) {
  const [collapsed, setCollapsed] = useState(
    () => initialCollapsed ?? loadCollapsed(),
  );
  // Wraps the chip AND the panel — see dismissOnOutside.ts on why the whole
  // host, not just the panel, is what counts as "inside".
  const hostRef = useRef<HTMLDivElement | null>(null);
  // Hand this poll's snapshot back to the queue half, so the run it is drawing is
  // retired against the same evidence this half is acting on rather than against a
  // read six seconds behind it (`QueueSlot.onJobs`, queue-dock-lib `openRows`). In an
  // effect and not in the render body: it sets state in the parent, and doing that
  // while rendering is what React warns about. Keyed on the array identity, which
  // changes exactly once per response or per local patch.
  //
  // The FULL, unfiltered snapshot — `isVanishedOnSuccess` below decides what
  // THIS card draws, not what the queue half (which reads its own `isRunning`
  // off this same list, queue-dock-lib `openRows`) is told about.
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
  //
  // `isVanishedOnSuccess` runs HERE, upstream of every decision below it — the
  // empty-card gate, the header count and its "N finished" tally, Clear's
  // count, the overall bar, the fold — so all of them agree a vanished row
  // does not exist, rather than a row that opens an empty `.dl-rows` box with
  // nothing visible inside it (the bug this comment used to leave standing:
  // `JobRow` alone returning null for a "done" job left every one of those
  // still counting it).
  // `inFlightJobs` — FAILURES ARE NOT DRAWN HERE ANY MORE (D586, user: "maybe
  // we can have a flow like running activities are shown in jobs and after
  // done, a completed message goes to notifications?"). An `error` row is not
  // work in progress, which is what this section claims to be, so it moves to
  // Notifications; `done`/`cancelled` are untouched and keep their existing
  // vanish-on-success and TTL behaviour. Applied HERE, upstream of every
  // derived number below — the count, `clearableCount`, the idle predicate,
  // the fold — so none of them can disagree about what this section holds
  // (the likeliest bug in this change was a count that still included
  // failures).
  const jobs = inFlightJobs(
    jobRows(reported, queue?.drawn).filter((j) => !isVanishedOnSuccess(j)),
  );
  const count: QueueCount = { waiting: queue?.waiting ?? 0, running: queue?.running ?? 0 };
  const queued = count.waiting + count.running;

  // Signals a genuinely new job id since the card was last collapsed —
  // lib/autoExpand.ts `useAutoExpandOnNew`'s own doc has the full reasoning,
  // including why this no longer FORCES the panel open (code review finding
  // #4: it used to, and popping a floating panel over the page the user is
  // looking at, unprompted, is the exact complaint this whole redesign
  // exists to fix). Called unconditionally, before the idle branch below,
  // same as every other hook in this component (rules of hooks: what a
  // render calls, not whether it later draws the idle state).
  const { autoOpen, autoClose, acknowledge, forceClose } = useAutoExpandOnNew(
    jobs.map((j) => j.id),
    collapsed,
    ready,
  );
  // OPEN is the persisted preference OR a transient auto-open (D574) — never
  // `collapsed` alone from here down, and the auto-open half is deliberately
  // not written back to localStorage (autoExpand.ts's own header on why that
  // write, not the opening, was D567's actual defect).
  // The saved preference, overridden in EITHER direction by whichever
  // transient flag is standing (D580 adds the closing half; the two are
  // mutually exclusive by construction — autoExpand.ts holds one `Override`,
  // not two independent booleans). `autoClose` is tested first because a
  // drained list beats a stale auto-open that the same drain is retiring.
  const open = autoClose ? false : !collapsed || autoOpen;

  // ONE panel at a time across the whole bar (D582). Only ever CLOSES this
  // section, and only transiently — see `exclusiveSection.ts` on why the
  // arbiter must not touch the saved preference.
  useExclusiveSection("jobs", open, forceClose);

  // ALWAYS PRESENT NOW (D565, superseding the empty-card gate this comment
  // used to describe): the bar's three sections are always on screen, this
  // one included, so "nothing happening" draws an IDLE chip — same button,
  // same hover wash, muted text (D573 retires the separate unpressable
  // `.dl-idle` span this used to render; see `.is-idle` below) — rather
  // than vanishing. Both halves empty is what decides idle, same test as
  // the old early return.
  const idle = jobs.length === 0 && queued === 0;

  // What "Clear" would actually take — TERMINAL rows only, mirroring the
  // server's own rule (jobs.py `clear_finished`). A stalled-but-running row
  // used to count here too; that silently orphaned live work behind the
  // button (`clearableCount`'s own doc has the full argument) — the button
  // hiding in that case is a far smaller cost than the AI job it used to
  // quietly abandon, and the per-row ✕ stays reachable for a stalled row
  // someone wants gone right now.
  const clearable = clearableCount(jobs);
  // THE FAILURE TINT IS GONE FROM THIS CHIP (D586). It used to colour the chip
  // `--error` when everything was terminal and something had failed — but
  // failures no longer appear in this section at all, so the condition could
  // never fire again and keeping it would have been dead code pretending to be
  // a state. The tint moved WITH the rows, to the Notifications chip
  // (RepoUpdatesDock.tsx), which is now the section that actually holds them.

  // ONE unified toggle for a chip whose visible state may be the SAVED
  // preference or either transient override (D580). It acts on what the user
  // SEES — `wantOpen = !open` — then writes the preference only if the
  // preference is what disagrees. That is what keeps D574's rule intact
  // without a special case for it: dismissing an auto-OPENED panel (or
  // reopening an auto-CLOSED one) finds the saved flag already agreeing with
  // the outcome, so clearing the override is the whole of the work and
  // nothing is persisted. A click on a chip whose state came from the
  // preference itself still flips and saves it, exactly as before.
  const toggle = () => {
    const wantOpen = !open;
    acknowledge();
    if (collapsed === wantOpen) {
      saveCollapsed(!wantOpen);
      setCollapsed(!wantOpen);
    }
  };

  // Backgrounding the panel (outside pointer-down, Escape). A hand-opened
  // panel persists the close, same as clicking the chip would; an auto-opened
  // one only drops the transient flag.
  // TRANSIENT ONLY — no write to the saved preference (D584 review finding 2).
  // `useDismissOnOutside` fires on any pointer-down outside THIS host, and a
  // click on a SIBLING CHIP is outside it, so the persisting version turned
  // "the user opened Models" into `jobs-collapsed = "1"` plus
  // `repo-updates-collapsed = "1"`. All three keys converged on "1" and the
  // preference became write-only — the exact "the app decided, not the user"
  // failure the D567 guard exists to prevent, arriving through the dismiss
  // path instead of through `forceClose`. So this now IS `forceClose`: the
  // panel goes away, and what the user last chose is left alone.
  const close = forceClose;
  useDismissOnOutside(hostRef, open, close);

  const clear = async () => {
    try {
      await clearFinishedJobs();
      // Mirrors the server's rule (jobs.py `clear_finished`, D558): every
      // running row survives, stalled included — see `clearableCount`'s doc.
      patch((js) => jobsAfterClear(js));
    } catch {
      /* nothing applied locally — the refresh below is the source of truth */
    }
    refresh();
  };

  // D573: the bar shows the category NAME plus a count, never a sentence —
  // `jobsSummary`'s richer "2 running · 1 queued" phrasing stays a pure,
  // fully-tested function (jobs.test.ts, queue-dock-lib.test.ts) but no
  // longer renders here; the count is simply every row this section is
  // showing, running/queued/terminal alike, which is also exactly what
  // `.dl-rows` below draws.
  const totalCount = jobs.length + queued;

  return (
    <div className="dl-host" ref={hostRef}>
      {/* ALWAYS a real, clickable button now (D573, user: "the chevron
          doesn't belong to the status bar. lets follow vscode/cursor for
          inspiration" — a status-bar item there is a label you click, not a
          disclosure triangle; hover is the only affordance, at rest and
          idle alike). `.is-idle` is the ONE remaining signal that this
          section has nothing in it — muted text, same clickable chip,
          same hover wash — now that the idle SENTENCE ("No jobs", D579) has
          moved into the panel below rather than living in the chip. */}
      <button
        className={"dl-toggle" + (idle ? " is-idle" : "")}
        onClick={toggle}
        aria-expanded={open}
        title={open ? "Hide jobs" : "Show jobs"}
      >
        {/* `Jobs`, NOT `Activity` (D579, user: "what about jobs?") — this
            codebase's own word for exactly this set (`fused_render/jobs.py`,
            `/api/jobs`, the `Job` dataclass, `KINDS`), so the label and the
            store it reads from finally agree. It also avoids the subset trap
            that ruled out `Downloads` and `Tasks`: downloads, background
            tasks AND the scheduled queue are all jobs, so it covers
            everything this section shows without over- or underclaiming.
            `Activity` was the vaguest of the three labels and half of why it
            collided with the old `Updates`. */}
        {/* The label, and the bar's one shared indicator beside it (D588,
            D590). `StatusDot` must stay a DIRECT child of this button — that
            is what centres it (its own header has the argument). */}
        <span className="dl-summary">Jobs</span>
        <StatusDot on={totalCount > 0} label={totalCount > 0 ? "jobs running" : "no jobs"} />
      </button>
      {/* The panel — floats ABOVE the status bar (notifications.css), anchored
          to this chip, and exists only while expanded: opening it IS collapsed
          turning false, there is no separate "peek" state. Collapsed shows NO
          panel at all (D562's "no exemption" carried forward into D563) — not
          a shorter one, an absent one, so `queue?.cancelAll` and `queue?.note`
          do NOT survive a collapse any more. UNLIKE round 3, the panel now
          opens on an IDLE section too (D573) — that is the one thing that
          changed here: an idle section used to skip the panel outright
          ("no panel behind it worth opening"); now the idle SENTENCE lives
          inside it, because the chip itself no longer has room to say it. */}
      {open && (
        <div className="dl-panel">
          {idle ? (
            <div className="dl-panel-empty">No jobs</div>
          ) : (
            <>
              {/* Omitted outright, not left as a blank padded band (code review
                  finding #3), when neither child has anything to offer — e.g.
                  exactly one job running, nothing queued, nothing terminal yet. */}
              {(queue?.cancelAll || clearable > 0) && (
                <div className="dl-head">
                  {/* Two actions, and they are not the same one twice: Cancel queued
                      withdraws messages that have not gone yet (the shell's, and only
                      when the shell decides enough rows are genuinely withdrawable —
                      `queue.cancelAll`'s own doc), Clear dismisses rows for work that
                      has ENDED. So a terminal row is clearable without a live one
                      being touched. */}
                  {queue?.cancelAll}
                  {clearable > 0 && (
                    <button className="dl-clear" onClick={clear} title="Dismiss finished">
                      Clear
                    </button>
                  )}
                </div>
              )}
              {/* ONE list, in lifecycle order: the queue's rows first (running, then
                  starting, then waiting) and the job rows under them, which is where
                  the same run lands once its turn has ended. A scheduled message
                  therefore moves down this list rather than jumping between two
                  cards. */}
              <div className="dl-rows">
                {queue?.rows}
                {jobs.map((job) => (
                  <JobRow key={job.id} job={job} onChanged={refresh} onPatch={patch} />
                ))}
              </div>
              {queue?.note}
            </>
          )}
        </div>
      )}
    </div>
  );
}

export default function DownloadManager({ queue }: { queue?: QueueSlot }) {
  const { jobs: reported, settled, refresh, patch } = useJobs();
  return (
    <DownloadManagerView
      reported={reported}
      ready={settled}
      queue={queue}
      refresh={refresh}
      patch={patch}
    />
  );
}
