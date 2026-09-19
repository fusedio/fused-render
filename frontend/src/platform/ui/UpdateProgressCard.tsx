// The self-update's progress, bottom right, while it runs (Akshil,
// 2026-09-19: "the download progress bar should be shown by default … like
// that notification let's show that as well, until the user closes it in
// bottom right"). It is the exact `JobRow` the Activity panel draws for the
// `sys:update:<version>` job — bytes, phase, Cancel — mounted in the floating
// column (`NotificationHost`) so the one install a person waits on is on
// screen without opening Activity. The Activity panel itself still never
// auto-opens (D587): this is one row for one job, not the panel.
//
// UNLIKE `JobPopupCard` THIS CARD HAS NO CLOCK AND NO OUTSIDE-PRESS EXIT: a
// download is minutes, not seconds, and "until the user closes it" means the
// ✕ — or the job leaving the running set (done → the ordinary terminal pop-up
// takes over; installed → the blocking restart dialog does). Closing is
// remembered per JOB ID for the page's lifetime, so a dismissed card does not
// come back on the next poll but a later version's install gets its own.
//
// THE ✕ ONLY CLOSES THE CARD — `onDismissClick` overrides `JobRow`'s
// server-side dismiss, same rule as the pop-up: swatting the card away is "I
// saw this", not "cancel the update". Cancel is Cancel, on the row.
import { useState } from "react";
import { JobRow } from "@platform/ui/DownloadManager";
import type { Job } from "@platform/lib/jobs";

const NOOP = () => {};

export default function UpdateProgressCard({ job }: { job: Job }) {
  const [closedId, setClosedId] = useState<string | null>(null);
  if (closedId === job.id) return null;
  return (
    <div className="toast-slot update-progress-card">
      <JobRow job={job} onChanged={NOOP} onPatch={NOOP} onDismissClick={() => setClosedId(job.id)} />
    </div>
  );
}
