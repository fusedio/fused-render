// The activity card — ONE card at the foot of the notification stack for every
// piece of work in progress: the long-running operations any page reported
// (SPEC §36, D244). A scheduled message's own run is NOT among them (D655,
// user: "a task is not something I even want in the activity. that was added
// unintentionally") — it gets its own toast on finish/fail
// (platform/lib/schedule-toast.ts), never a row here.
//
// STATUS-BAR MERGE, THEN A PARTIAL REVERT: this chip absorbed the two other
// PERSISTENT-status chips that used to sit beside it — Models
// (shell/ModelsDock.tsx) and Engines (shell/EnginesDock.tsx), both deleted at
// the time. A follow-up revision then split Models back out into its own chip
// (shell/ModelsDock.tsx again, resurrected) because the user relies on that
// chip's own filled/outlined dot to know whether the machine is holding any
// model weights, and a dot shared with jobs/engines answered a different
// question. Engines stayed merged here — nothing comparable was ever asked of
// its own indicator. So this panel draws up to TWO labelled sections in order
// — Running (job rows), then Background tasks (engine rows) — a section only
// when it has rows, a heading only when 2+ sections are present at once. See
// `EnginesSlot` below for why the row rendering could move into platform (it
// only needs `platform/lib/api` types) while the poller that FEEDS it stayed
// in shell (`shell/ActivityDock.tsx`, the sole caller of this component). The
// chip's own `StatusDot` still answers a narrower question than "does this
// chip have anything to show": see `runningCount`, below, for why a running
// engine alone leaves it unfilled.
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
// A JOB LEAVES THIS CARD THE MOMENT IT IS TERMINAL (D656, broadening D586):
// "running activities are shown in jobs and after done, a completed message
// goes to notifications" — the whole sentence, not only its failure half.
// `inFlightJobs` (jobs.ts) is what this card draws; `RepoUpdatesDock.tsx`
// draws every job `inFlightJobs` excludes, off the same `onJobsReported`
// snapshot this card forwards on every poll.
//
// EVERYTHING FOLDS NOW (D562, user call 2026-08-27, reversing D558/D559's
// exemptions): collapsing hides every row, with no kind pinned outside it.
// The user's rule is that there is no such thing as a "non-foldable card",
// full stop.
//
// COLLAPSED IS NOW A CHIP, NOT A SHORT CARD (D563, status bar redesign, user
// call: "the collapsed notification is also taking too much space... it is
// impossible to use the claude template with it"). `.dl-toggle` — the
// category name, its count (D573), the aggregate percentage — is the WHOLE
// of what renders while collapsed; every row renders only inside the panel
// that opens when the card is expanded, and so does not survive a collapse.
// That reachability requirement is gone on purpose, not merely forgotten: the
// chip's whole point is to cost the page nothing but one summary line, and a
// button on it would be a second thing competing for that line's very little
// room. Nothing here rewrites the stored preference; it is only ever changed
// by the user pressing the chip.
//
// PLACEMENT MOVED (D563): this card used to sit in NotificationHost's fixed,
// floating column with the toasts and the server card, ABOVE the server card
// and BELOW the toasts by lifetime (a toast is seconds, work in progress is
// minutes, the server card outlives the session) — but a FIXED column
// overlays page content even collapsed, which is what made a page like the
// Claude template unusable under it. It is handed to `StatusBar` now instead,
// mounted inside `#main` where it RESERVES layout space rather than floating
// over it; the toasts and `ServerStatusBanner` are unaffected and
// stay in NotificationHost's column, since none of them are long-lived
// enough to be worth a permanently reserved strip. This component still
// positions nothing itself — `StatusBar` owns the chip's place in the bar,
// and this file's own CSS classes own the panel floating above it.
//
// Cancel is a REQUEST, not a kill (jobs.py `request_cancel`): the shell has no
// idea what the work is or which process is doing it, so the ✕ sets a flag the
// reporting page reads on its next tick and acts on. The row therefore says
// "Cancelling…" until the work actually stops, rather than lying about it.
import { useCallback, useEffect, useRef, useState } from "react";
import { useAutoExpandOnNew } from "@platform/lib/autoExpand";
import StatusDot from "@platform/ui/StatusDot";
import { useExclusiveSection } from "@platform/lib/exclusiveSection";
import { useDismissOnOutside } from "@platform/lib/dismissOnOutside";
import {
  cancelJob,
  dismissJob,
  engineDuration,
  fetchJobs,
  isRunning,
  jobAmount,
  jobDetail,
  jobFraction,
  jobRows,
  inFlightJobs,
  mergedRows,
  jobStatusLine,
  pollInterval,
  JOB_PING_KEY,
  type Job,
} from "@platform/lib/jobs";
import { repoName } from "@platform/lib/format";
import type { RunningEngine } from "@platform/lib/api";
// NOTHING ABOUT THE FOLD IS PERSISTED (D603, user: "on page reload the models
// popover auto opens for some reason"). There used to be a `COLLAPSED_KEY` here
// plus `loadCollapsed`/`saveCollapsed`; all three are DELETED, not merely
// unread — a key that is written and never read is worse than no key, because
// the next reader assumes it means something.
//
// WHY: a `.dl-panel` floats above the page and is dismissed by an outside
// pointer-down or Escape. That is popover behaviour, and a popover that
// restores itself across reloads covers the page on every navigation. "Open"
// is a statement about this moment, not a preference worth remembering. The
// user's own report was not the auto-open path at all — D587's `neverOpen` was
// intact — it was a stored `"0"` from having clicked Models open earlier,
// faithfully restored on every load since, which is indistinguishable from a
// bug from where they sit. This also makes D582's arbiter trivial instead of
// arbitrary (nothing wants to be open at mount) and finally makes "never auto
// open" hold on EVERY path rather than all but one.
//
// The transient `autoOpen`/`autoClose` overrides are untouched; opening is an
// explicit click within the session. Any key left on a real machine from an
// earlier build is inert and needs no migration — nothing reads it.

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

// ---- Engine rows (status-bar merge) ----------------------------------------
// FORMERLY shell/EnginesDock.tsx, its own chip/panel. The status-bar
// consolidation collapsed Engines and Jobs into one "Activity" chip (see
// DownloadManagerView's own header for the section layout), so the row
// rendering moved here, alongside the job rows it now shares a panel with.
// (Models made the same trip during the merge and then made it back: see
// this file's own header comment and `shell/ModelsDock.tsx`.) This is legal
// under `check-boundaries.mjs`: these rows only need `RunningEngine`
// (platform/lib/api, which platform already owns) and the mutation call
// `stopEngine` (also platform/lib/api) — nothing shell-only. Only the DATA
// SOURCE for them — the running-engines poll — is shell-only, and it stays in
// `shell/ActivityDock.tsx`, which hands this component plain
// `RunningEngine[]` data plus the mutation callback.

/** A useful NAME for an engine row, never the opaque id when something better
 *  exists: the folder's basename for a background app (`daemon =` or
 *  `main =` alike), falling back to the module for a background app with no
 *  folder recorded, and the id itself for a template engine. Pure and
 *  exported so it is testable without a render. */
export function engineLabel(engine: RunningEngine): string {
  if (engine.folder) {
    const parts = engine.folder.split(/[/\\]/).filter(Boolean);
    if (parts.length > 0) return parts[parts.length - 1];
  }
  return engine.module || engine.engine_id;
}

/** WHICH OF THE THREE KINDS of child this row is, from the two fields the
 *  server already sends (`running_engines`): a `module` set means the shipped
 *  worker is running that module's `main(**params)` — a WORKER whether or not
 *  a folder came with it (`ensure_background(..., folder="")` defaults
 *  `folder`, so a `main =` app can report one either way); otherwise a folder
 *  is an app's own written daemon, and no folder at all is a built-in
 *  template's daemon. `module` is checked first because a template child can
 *  never carry one (`ensure()` never sets it) — checking `folder` first, as
 *  this used to, mislabelled a folder-less worker as a template, disagreeing
 *  with `engineLabel` above about the very same row. Derived here rather than
 *  added to the wire because the two fields already determine it — a `kind`
 *  field would be a second answer to one question, free to disagree. */
export function engineKind(engine: RunningEngine): "app" | "worker" | "template" {
  if (engine.module) return "worker";
  return engine.folder ? "app" : "template";
}

const KIND_TEXT = {
  app: "Background app",
  worker: "Warm worker",
  template: "Template engine",
} as const;

/** The sentence under an engine row's name — what this daemon IS, how long it
 *  has been up, and what will end it (user call: "can we also add some more
 *  context regarding the activity. if a daemon has a timeout, lets also
 *  mention that"). A row used to be a bare folder name beside a Stop button,
 *  which said nothing about why a process the user never started was running
 *  or when it would go away on its own.
 *
 *  The last clause is the whole point and has three cases, in the order they
 *  answer "when does this stop":
 *    * no timeout at all — a resident daemon stays until it is stopped, and
 *      saying so is the difference between "deliberate" and "leaked";
 *    * a call in flight — the countdown is not running (`reap_idle_children`
 *      skips a busy child), so a frozen "retires in" would read as a bug;
 *    * otherwise the countdown itself, hedged with "if idle" because any
 *      call landing before it expires resets it.
 *
 *  Pure and exported so the wording is testable without a render. */
export function engineDetail(engine: RunningEngine): string {
  const parts = [KIND_TEXT[engineKind(engine)], `up ${engineDuration(engine.uptime_s)}`];
  if (engine.idle_timeout_s <= 0) {
    parts.push("no idle timeout");
  } else if (engine.busy) {
    parts.push(`in use · idle timeout ${engineDuration(engine.idle_timeout_s)}`);
  } else {
    const left = engine.idle_timeout_s - engine.idle_for_s;
    parts.push(left <= 0 ? "retiring now" : `retires in ${engineDuration(left)} if idle`);
  }
  return parts.join(" · ");
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

  // Rows are keyed `key={e.engine_id}` (below), so this component stays
  // mounted — and `failure` stays set — for the whole life of a running
  // engine, across every 10s poll. A fresh `engine` prop IS the signal that
  // the poll landed again (`shell/ActivityDock.tsx` always hands down a
  // newly-fetched object), so clearing here on its arrival, rather than
  // leaving a once-failed Stop stuck on the row forever, is what makes the
  // kind/uptime/retire line come back once connectivity does.
  useEffect(() => {
    setFailure(null);
  }, [engine]);

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
    <div className="dl-row">
      <div className="dl-row-head">
        <span
          className="dl-title dl-title-id"
          title={`${engine.folder || engine.engine_id} — pid ${engine.pid}`}
        >
          {engineLabel(engine)}
        </span>
        <button className="dl-row-cancel" onClick={stop} disabled={busy}>
          {busy ? "Stopping…" : "Stop"}
        </button>
      </div>
      {/* The failure REPLACES the detail line rather than stacking under it:
          a row whose Stop just failed has one thing worth reading. It clears
          itself on the next poll (the `useEffect` above), so it stays there
          only until the row has something fresh to say. */}
      <div className="dl-status">{failure || engineDetail(engine)}</div>
    </div>
  );
}

/** The `engines` slot (status-bar merge): plain data plus the one mutation
 *  the row needs, handed in by `shell/ActivityDock.tsx` (the only place the
 *  poll lives). Ids are for occupancy only (`alsoDrawn` below, in the merged
 *  panel's auto-expand wiring) — an engine arriving must never itself pop the
 *  panel open (D587's rule, now shared by the merged chip: only Running/job
 *  arrivals announce). */
export interface EnginesSlot {
  engines: RunningEngine[];
  onStop: (engineId: string) => Promise<void>;
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
  // THE PROGRESS FACTS TOGETHER, dot-joined (D598, user: "why isn't the step
  // count next to denoising?"). `0 / 4` is a progress fact, so it belongs with
  // the other progress facts rather than up beside the title, where it was a
  // number with no context while the status line carried context with no
  // number. The line already speaks this idiom — it rendered `Denoising · ~4s
  // left` before this round — so `Denoising · 0/4` reads as one sentence.
  //
  // COMPOSED, NOT CONCATENATED BEHIND THE OLD GUARD: `amount` and `status` are
  // independently present or absent. A download row can carry `4.2 GB / 10 GB`
  // with NO phase text, and a task row a phase with no amount, so the old
  // `(failure ?? status) &&` gate would have silently dropped a download row's
  // byte counts entirely — a straight regression, and worse than what this
  // fixes. Hence: render whenever ANY part exists, and join only the parts
  // that do.
  //
  // A FAILURE TAKES THE LINE OVER and gets NO amount appended (the precedence
  // the comment below has always stated): it is about the button the user just
  // pressed, and " · 0/4" after "Could not cancel" would read as progress on
  // the failure itself.
  // `??` once, not twice: `join` returns "" rather than null for an empty
  // list, so a second `??` would be dead — the `&&` at the render site is what
  // handles the all-absent case.
  // No card may render a single line of text (title alone, nothing beneath
  // it) (D659): a running job with no server detail and no progress amount
  // falls through to `jobDetail`'s always-present facts (kind, started-when,
  // stalled) rather than leaving the row silent.
  const statusLine = failure ?? ([status, amount].filter(Boolean).join(" · ") || jobDetail(job));

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

  // NO EXEMPTION FOR "done" HERE (C1 fix): `JobRow` is reused verbatim by
  // `RepoUpdatesDock.tsx` to draw every terminal job — done, error and
  // cancelled alike — in Notifications, and a `done` job returning null left
  // that panel counting it (`total`, the filled circle, `alsoDrawn`
  // occupancy) while drawing nothing for it: a phantom entry with no row and
  // no ✕, permanent once D657 stopped sweeping it. Keeping a terminal job out
  // of THIS file's own Jobs section is `DownloadManagerView`'s job — it only
  // ever hands `JobRow` `inFlightJobs`, so a "done" row never reaches this
  // component from there at all.
  return (
    <div className={"dl-row" + (job.stalled ? " is-stalled" : "")}>
      <div className="dl-row-head">
        <span className="dl-title" title={job.page || undefined}>
          {job.title}
        </span>
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
      {statusLine && <div className="dl-status">{statusLine}</div>}
    </div>
  );
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
  engines,
  onJobsReported,
  refresh,
  patch,
}: {
  reported: Job[];
  /** TEST SEAM ONLY — the fold's initial value. Every real caller omits it and
   *  gets `true`: sections ALWAYS start collapsed now (D603), unconditionally,
   *  with no stored preference to consult. KEPT rather than deleted with the
   *  persistence, because it is now the ONLY way to mount a section already
   *  open, and ~20 tests here are about what an OPEN panel contains rather than
   *  about the default. Injectable rather than stubbed through
   *  `globalThis.localStorage` for the reason this file documents at length for
   *  `mock.module`: a process-wide replacement has contaminated unrelated
   *  suites here before. */
  initialCollapsed?: boolean;
  /** Has the first /api/jobs read landed (autoExpand.ts's `ready`)? Optional
   *  so a test mounting this view with a fixed list keeps the old behaviour. */
  ready?: boolean;
  /** The Background tasks section (formerly EnginesDock's own chip). Optional
   *  and data-only — see `EnginesSlot`'s own doc. */
  engines?: EnginesSlot;
  /** The FULL, unfiltered job snapshot this poll landed, forwarded on every
   *  poll for a consumer that needs to know about a job leaving this card —
   *  `shell/ActivityDock.tsx` uses it to route every terminal job into
   *  Notifications (D586, broadened) and to toast an idle-retired engine.
   *  Independent of what this card itself draws (`jobs` below): a job this
   *  card never shows (a scheduled run) still needs to reach that consumer. */
  onJobsReported?: (jobs: Job[]) => void;
  refresh: () => void;
  patch: (fn: (jobs: Job[]) => Job[]) => void;
}) {
  const [collapsed, setCollapsed] = useState(initialCollapsed ?? true);
  // Wraps the chip AND the panel — see dismissOnOutside.ts on why the whole
  // host, not just the panel, is what counts as "inside".
  const hostRef = useRef<HTMLDivElement | null>(null);
  // Forward the FULL snapshot to whoever asked (`onJobsReported`), independent
  // of `jobs` below (what this card draws). In an effect and not in the render
  // body: it can set state in a parent, and doing that while rendering is what
  // React warns about. Keyed on the array identity, which changes exactly once
  // per response or per local patch.
  useEffect(() => {
    onJobsReported?.(reported);
  }, [onJobsReported, reported]);
  // Everything the poll returned MINUS a scheduled run's own job (`jobRows`,
  // never drawn here — user: "a task is not something I even want in the
  // activity") MINUS every terminal job (`inFlightJobs` — D586, broadened:
  // "running activities are shown in jobs and after done, a completed
  // message goes to notifications", now for ALL of done/error/cancelled, not
  // only error). Applied here, upstream of every derived value below — the
  // header count, the idle predicate, the fold — so none of them can
  // disagree about what this section holds.
  //
  // `mergedRows` runs FIRST, on the full `reported` snapshot rather than on
  // its filtered output — it needs the REFERENCING row (the waiter,
  // `waiting_for`-tagged) still present to decide whether the row it names is
  // hidden, and `jobRows`/`inFlightJobs` never remove that row on their own.
  // This is what collapses a render waiting on a shared model load and the
  // load's own row into the one row the manager actually draws (SPEC §36;
  // jobs.ts `mergedRows` has the full reasoning).
  const jobs = inFlightJobs(jobRows(mergedRows(reported)));

  // Signals a genuinely new job id since the card was last collapsed —
  // lib/autoExpand.ts `useAutoExpandOnNew`'s own doc has the full reasoning,
  // including why this no longer FORCES the panel open (code review finding
  // #4: it used to, and popping a floating panel over the page the user is
  // looking at, unprompted, is the exact complaint this whole redesign
  // exists to fix). Called unconditionally, before the idle branch below,
  // same as every other hook in this component (rules of hooks: what a
  // render calls, not whether it later draws the idle state).
  //
  // ENGINE ROWS COUNT FOR OCCUPANCY, NEVER ANNOUNCE (status-bar merge): an
  // engine coming up is a state readout, the same way it was in its own
  // now-deleted chip (D587's "never auto-opens" rule), so only a job arrival
  // may set `ids` here. A terminal job never reaches `jobs` above at all —
  // this card draws nothing for it to occupy — so nothing about a terminal
  // job goes into `alsoDrawn` here either; that guarantee belongs to
  // `RepoUpdatesDock.tsx`, the panel that actually draws those rows.
  const { autoOpen, autoClose, acknowledge, forceClose } = useAutoExpandOnNew(
    jobs.map((j) => `job:${j.id}`),
    collapsed,
    ready,
    { alsoDrawn: (engines?.engines ?? []).map((e) => `engine:${e.engine_id}`) },
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
  useExclusiveSection("activity", open, forceClose);

  // ALWAYS PRESENT NOW (D565, superseding the empty-card gate this comment
  // used to describe): the bar's sections are always on screen, this one
  // included, so "nothing happening" draws an IDLE chip — same button, same
  // hover wash, muted text (D573 retires the separate unpressable `.dl-idle`
  // span this used to render; see `.is-idle` below) — rather than vanishing.
  //
  // EVERYTHING THIS CHIP CAN SHOW decides idle/muting (status-bar merge): a
  // running engine alone is no longer nothing, now that this chip shows it
  // too. `runningCount` below is a narrower question — is there WORK — and
  // stays scoped to jobs/queue only.
  const engineCount = engines?.engines.length ?? 0;
  const idle = jobs.length === 0 && engineCount === 0;

  // NO CLEAR BUTTON HERE ANY MORE: `jobs` above is `inFlightJobs` — running or
  // waiting only, never terminal — so there is never a terminal row for a
  // bulk Clear to take. Every terminal job now lives in `RepoUpdatesDock.tsx`
  // instead, one dismiss ✕ per row, same as a failure always worked.
  //
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
    if (collapsed === wantOpen) setCollapsed(!wantOpen);
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

  // THE DOT ANSWERS "IS THERE WORK RIGHT NOW", NOT "IS THERE ANYTHING TO SHOW"
  // (status-bar merge, brief's own rule): jobs running or waiting fill it; a
  // running engine — persistent STATE, not work in progress — does not, even
  // though the panel still shows it when opened and the chip is not muted for
  // having it (see `idle`, above, which DOES count it). A machine running a
  // background engine and no jobs therefore draws an active, clickable
  // "Activity" chip with an outlined (unfilled) dot.
  const runningCount = jobs.length;

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
        title={open ? "Hide activity" : "Show activity"}
      >
        {/* `Activity` (status-bar merge): this chip is no longer only jobs —
            it now also carries the running engines that used to be their own
            chip beside it, so `Jobs` (D579's own word for the narrower set)
            no longer names everything it shows. `Activity` was rejected back
            then for being the vaguest of three labels over a chip that was
            ONLY jobs; it is the right word again now that the chip covers
            both. (Models made this same trip during the merge and then split
            back out into its own chip — `shell/ModelsDock.tsx` — so it is not
            one of the things "Activity" has to cover any more.) */}
        {/* The label, and the bar's one shared indicator beside it (D588,
            D590). `StatusDot` must stay a DIRECT child of this button — that
            is what centres it (its own header has the argument). It answers
            "is there work right now" — see `runningCount`, above — not "is
            there anything to show", which is `idle`'s question. */}
        <span className="dl-summary">Activity</span>
        <StatusDot
          on={runningCount > 0}
          label={runningCount > 0 ? "jobs running" : "no jobs running"}
        />
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
            <div className="dl-panel-empty">No activity</div>
          ) : (
            <>
              {/* TWO POSSIBLE SECTIONS, in this order (status-bar merge):
                  Running (the old Jobs chip's own content, unchanged) and
                  Background tasks (the old Engines chip). A section renders
                  only when it has rows, and the heading itself only when 2+
                  sections are present at once — a single section carrying a
                  header nobody needed to disambiguate is the redundant-label
                  problem the brief calls out; see `.dl-section-head` in
                  notifications.css. (A third section, Models, lived here
                  during the status-bar merge and moved back out into its own
                  chip — `shell/ModelsDock.tsx` — in a follow-up revision.) */}
              {(() => {
                const runningVisible = jobs.length > 0;
                const sectionCount = (runningVisible ? 1 : 0) + (engineCount > 0 ? 1 : 0);
                const showHeadings = sectionCount > 1;
                return (
                  <>
                    {runningVisible && (
                      <div className="dl-section">
                        {showHeadings && <div className="dl-section-head">Running</div>}
                        <div className="dl-rows">
                          {jobs.map((job) => (
                            <JobRow key={job.id} job={job} onChanged={refresh} onPatch={patch} />
                          ))}
                        </div>
                      </div>
                    )}
                    {engineCount > 0 && (
                      <div className="dl-section">
                        {showHeadings && <div className="dl-section-head">Background tasks</div>}
                        <div className="dl-rows">
                          {engines!.engines.map((e) => (
                            <EngineRow key={e.engine_id} engine={e} onStop={engines!.onStop} />
                          ))}
                        </div>
                      </div>
                    )}
                  </>
                );
              })()}
            </>
          )}
        </div>
      )}
    </div>
  );
}

export default function DownloadManager({
  engines,
  onJobsReported,
}: {
  engines?: EnginesSlot;
  onJobsReported?: (jobs: Job[]) => void;
}) {
  const { jobs: reported, settled, refresh, patch } = useJobs();
  return (
    <DownloadManagerView
      reported={reported}
      ready={settled}
      engines={engines}
      onJobsReported={onJobsReported}
      refresh={refresh}
      patch={patch}
    />
  );
}
