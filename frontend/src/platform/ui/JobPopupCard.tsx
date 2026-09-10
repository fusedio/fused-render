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
// OPENING THE CARD CLOSES IT EARLY. `JobRow`'s own `open()` (commit
// ba64d03f9, "opening a row always dismisses it") already calls `dismiss()`
// on click — reused as-is rather than reimplemented, per item 7 of the
// brief this shipped against. `JobRow` has no "I was dismissed" callback of
// its own, so `onPatch` is read as that signal instead: cancel is never
// offered here (`JobRow`'s `canCancel` requires `running`, and every job
// this card ever holds is terminal), so the only patch `JobRow` can ever
// invoke on a card mounted here is dismiss's own `filter`. A `transient`
// job's dismiss still succeeds server-side (`fused_render/jobs.py`'s
// `dismiss` takes any terminal record regardless of tier) even though it
// never had a panel row to clear — the click closes the popup exactly the
// same way either way, no special-casing needed for tier here.
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
  useEffect(() => {
    const t = window.setTimeout(() => setLeaving(true), JOB_POPUP_VISIBLE_MS);
    return () => window.clearTimeout(t);
  }, []);

  useEffect(() => {
    if (!leaving) return;
    const t = window.setTimeout(() => goneRef.current(), TOAST_EXIT_MS);
    return () => window.clearTimeout(t);
  }, [leaving]);

  return (
    <div className={"toast-slot" + (leaving ? " leaving" : "")}>
      <JobRow
        job={job}
        onChanged={NOOP}
        onPatch={() => setLeaving(true)}
        cancelFn={cancelFn}
        dismissFn={dismissFn}
      />
    </div>
  );
}
