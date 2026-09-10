// The floating pop-up half of a terminal job's notification (SPEC
// actionable-notifications, user: "when getting notifications, ensure the
// latest notification always pops up and auto disappears under 3 seconds.
// they still stay in the list"). It is the exact `JobRow` the Notifications
// panel draws for the same job — reused verbatim, the same way
// `shell/RepoUpdatesDock.tsx` reuses it for its own terminal rows — mounted
// here for `JOB_POPUP_VISIBLE_MS` and then playing `lib/toast`'s own
// grid-collapse exit (`TOAST_EXIT_MS`) before calling `onGone`. The row this
// job may also have in the panel (kept or not, per its tier — see `JobTier`
// in jobs.ts) is untouched either way: this card is a second, temporary way
// to see the SAME notification, never a second copy of it.
//
// CLICKING THE ROW opens `job.page` and dismisses it, exactly as `JobRow`'s
// own click handler always does — going to look is the acknowledgement, the
// same rule Notifications itself uses, so the panel row (if this job has
// one) really does clear.
//
// THE ✕ ONLY CLOSES THE CARD. It does not touch the panel: swatting away a
// pop-up is "I saw this, stop showing it to me", not "delete the
// Notifications row for it", so `onDismissClick` overrides `JobRow`'s
// ordinary ✕ to skip the real, server-side dismiss and just start this
// card's own exit animation instead. A job with nothing kept in the panel
// (a `transient` tier) loses nothing either way; a job that IS kept (an
// `attention`/`trail` row) stays there for the user to act on later — the
// one thing "they still stay in the list" requires.
//
// A PRESS ANYWHERE ELSE also starts the same exit — see the outside-press
// effect below for why that never disturbs the press itself.
import { useEffect, useRef, useState } from "react";
import { JobRow } from "@platform/ui/DownloadManager";
import { JOB_POPUP_VISIBLE_MS, type Job } from "@platform/lib/jobs";
import { TOAST_EXIT_MS } from "@platform/lib/toast";

const NOOP = () => {};

export default function JobPopupCard({
  job,
  onGone,
  cancelFn,
  dismissFn,
}: {
  job: Job;
  onGone: () => void;
  /** Test seam only, threaded straight through to `JobRow`'s own identical
   *  seam (JobPopupCard.test.tsx) — every real caller omits both and gets
   *  `JobRow`'s real `cancelJob`/`dismissJob` defaults. */
  cancelFn?: (id: string) => Promise<Job>;
  dismissFn?: (id: string) => Promise<{ dismissed: string }>;
}) {
  const [leaving, setLeaving] = useState(false);
  // Read by the exit timer without re-arming it on every render — `onGone`
  // is a fresh closure from `NotificationHost` on each of its own renders,
  // and this card must not restart its countdown just because its parent
  // re-rendered for an unrelated reason.
  const goneRef = useRef(onGone);
  goneRef.current = onGone;

  // `NotificationHost` mounts this with `key={job.id}` (App.tsx), so a new
  // job is always a fresh instance of this component — this effect runs
  // exactly once per card's whole life, never restarting mid-flight for the
  // same job.
  //
  // `globalThis.setTimeout`/`globalThis.clearTimeout`, not `window`'s — this
  // is the same exit-timing shape `lib/toast.ts` already documents at length:
  // a timer scheduled here through `window` fired inside a later bun test
  // file with no DOM shim installed, and `window is not defined` aborted the
  // whole run between files rather than failing the one test that owned it.
  useEffect(() => {
    const t = globalThis.setTimeout(() => setLeaving(true), JOB_POPUP_VISIBLE_MS);
    return () => globalThis.clearTimeout(t);
  }, []);

  useEffect(() => {
    if (!leaving) return;
    const t = globalThis.setTimeout(() => goneRef.current(), TOAST_EXIT_MS);
    return () => globalThis.clearTimeout(t);
  }, [leaving]);

  // A press anywhere else is "I saw this, move on" — the same acknowledgement
  // clicking the row already is, just aimed somewhere other than the row.
  // CAPTURE phase, and neither `preventDefault` nor `stopPropagation` is ever
  // called: whatever the user actually pressed (a menu item, a link, another
  // card) still gets the event exactly as if this card were not here. Only
  // the card's OWN exit starts; the press itself is never disturbed.
  //
  // `cardRef` + `contains(e.target)` is what tells an outside press apart
  // from one that landed on the row itself — the row's own click handler
  // (onDismissClick/onPatch above) already starts the same `leaving` state,
  // so a press inside the card is left alone here rather than raced against.
  //
  // `globalThis.addEventListener`/`removeEventListener`, not `window`'s or
  // `document`'s — the same reason the timers above read `globalThis`:
  // `window`/`document` are no-op stubs in the test shim
  // (testDomShim.ts), while Bun's `globalThis` is a real `EventTarget`, so a
  // listener attached here is the one a test can actually exercise.
  //
  // No `blur` listener: a floating pop-up in the SAME window as everything
  // else only ever loses focus for reasons that have nothing to do with this
  // card — alt-tabbing away, opening devtools, a native file picker — and
  // window blur fires for all of them alike. Wiring it here would hide the
  // card out from under a user who never pressed anything near it, which is
  // worse than the gap it would close: the one case blur would actually
  // cover (a job started from a separate-document panel/tab pane) is left
  // uncovered rather than risk that.
  const cardRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (leaving) return;
    const onOutside = (e: Event) => {
      const card = cardRef.current;
      const target = e.target as Node | null;
      if (card && target && card.contains(target)) return;
      setLeaving(true);
    };
    globalThis.addEventListener("pointerdown", onOutside, true);
    return () => globalThis.removeEventListener("pointerdown", onOutside, true);
  }, [leaving]);

  return (
    <div ref={cardRef} className={"toast-slot" + (leaving ? " leaving" : "")}>
      <JobRow
        job={job}
        onChanged={NOOP}
        onPatch={() => setLeaving(true)}
        onDismissClick={() => setLeaving(true)}
        cancelFn={cancelFn}
        dismissFn={dismissFn}
      />
    </div>
  );
}
