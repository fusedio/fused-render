// The floating pop-up half of a terminal job's notification (SPEC
// actionable-notifications, user: "when getting notifications, ensure the
// latest notification always pops up and auto disappears under 3 seconds.
// they still stay in the list"). It is the exact `JobRow` the Notifications
// panel draws for the same job — reused verbatim, the same way
// `shell/RepoUpdatesDock.tsx` reuses it for its own terminal rows — mounted
// here for `JOB_POPUP_VISIBLE_MS` and then playing `lib/notifications`'s own
// grid-collapse exit (`TOAST_EXIT_MS`) before calling `onGone`. The row this
// job may also have in the panel (kept or not, per its tier — see `JobTier`
// in jobs.ts) is untouched either way: this card is a second, temporary way
// to see the SAME notification, never a second copy of it.
//
// CLICKING THE ROW opens `job.page` and dismisses it, exactly as `JobRow`'s
// own click handler always does — going to look is the acknowledgement, the
// same rule Notifications itself uses, so the panel row (if this job has
// one) really does clear. The card itself has no reach into the shell's own
// terminal-jobs list, though (App.tsx owns that, several components up), so
// its `onPatch` also calls `jobs.ts`'s `noteJobDismissed` — the one thing
// telling the panel this id is really gone, promptly, rather than leaving it
// there until the next Activity poll happens to notice.
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
import { useRef, useState } from "react";
import { JobRow } from "@platform/ui/DownloadManager";
import { JOB_POPUP_VISIBLE_MS, noteJobDismissed, type Job } from "@platform/lib/jobs";
import { TOAST_EXIT_MS } from "@platform/lib/notifications";
import { usePopupCardLifecycle } from "@platform/ui/usePopupCardLifecycle";

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
  const cardRef = useRef<HTMLDivElement | null>(null);

  // The mount-once timer, the exit timer, outside-press dismissal and the
  // iframe-blur edge case — all four extracted to `usePopupCardLifecycle`
  // (SPEC-toasts-become-notifications.md §2) so `MessagePopupCard.tsx`
  // shares this exact lifecycle rather than re-implementing it. `visibleMs`
  // is never `null` here: only a message popup (not a job's) can outlive its
  // own timer, under `IS_TOP_EMBED` — see that hook's own doc comment.
  usePopupCardLifecycle({
    cardRef,
    leaving,
    setLeaving,
    onGone,
    visibleMs: JOB_POPUP_VISIBLE_MS,
    exitMs: TOAST_EXIT_MS,
  });

  return (
    <div ref={cardRef} className={"toast-slot" + (leaving ? " leaving" : "")}>
      <JobRow
        job={job}
        onChanged={NOOP}
        // `onPatch` only ever runs here on `dismiss()`'s success path — a
        // popup only ever shows a terminal job, so the cancel button (the
        // only other caller of `onPatch`) never renders on one. Closing
        // this card is this card's OWN business (`setLeaving`), but the
        // dismissal itself is real and server-side, and this card has no
        // reach into the shell's own terminal-jobs list — `noteJobDismissed`
        // is how that list finds out promptly instead of waiting for its
        // next poll to notice the row gone.
        onPatch={() => {
          setLeaving(true);
          noteJobDismissed(job.id);
        }}
        onDismissClick={() => setLeaving(true)}
        cancelFn={cancelFn}
        dismissFn={dismissFn}
      />
    </div>
  );
}
