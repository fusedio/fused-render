// The single notification surface: one fixed, bottom-right column holding
// every transient toast (lib/toast), the download manager, and the
// server-health card pinned at its foot. Mounted once by App.
//
// It replaced three competing surfaces — a bottom-centre global toast stack, a
// per-pane toast each of Listing and Preview positioned and expired itself,
// and this bottom-right card — which between them meant the same "Path copied"
// appeared in two different places depending on which view raised it, and a
// toast could sit next to (or under) an unrelated card in the other corner.
// One stack, one set of stacking rules, one auto-dismiss timer (the store's).
//
// Order is oldest → newest top to bottom, so the newest message is nearest the
// bottom edge where the eye already is, and the server card sits below all of
// them: it is the one entry that outlives any toast, so it must not shuffle as
// toasts come and go. Styling is .notif-host in shell.css.
//
// The column is ordered by LIFETIME, which is why the activity card (SPEC §36)
// sits between the toasts and the server card: a toast is seconds, work in
// progress is minutes, the server card outlives the session. Anything long-lived
// must be below anything short-lived, or a job's rows would shift under the
// pointer every time an unrelated "Path copied" arrived and expired.
//
// That is ONE entry in the column, not two. The scheduled-message queue used to
// take a card of its own directly above the manager and it is now folded into it
// (see DownloadManager's header): same corner, same plate, same kind of thing, so
// two headers over one lifecycle was the bug.
//
// Panes keep their attribution for free: in panel/tab mode each pane is its
// own document, so a pane's toast renders in THAT pane's bottom-right corner,
// not the window's. Only the top-level document shows the server card and the
// download manager (an embed would otherwise render one per pane, all saying
// the same thing — and the job list is global, so every copy would be
// identical).
import type { ReactNode } from "react";
import Toast from "@platform/ui/Toast";
import DownloadManager from "@platform/ui/DownloadManager";
import ServerStatusBanner from "@platform/ui/ServerStatusBanner";
import { dismissToast, useToasts } from "@platform/lib/toast";
import { IS_EMBED } from "@platform/lib/router";

// `activity` is the ONE work-in-progress card, handed in rather than imported
// because of the layering: its queue rows have to offer "Open in Explorer", the
// one answer to which lives in shell/schedule-lib (explorerUrl), and platform may
// not import shell (frontend/scripts/check-boundaries.mjs). So the shell composes
// the card (shell/QueueDock.tsx wraps DownloadManager with its queue slot) and
// this file keeps owning where the one entry sits.
//
// Omitted, the bare download manager stands in its place: platform is not made to
// depend on a shell that may not be there, and a host mounted without a scheduler
// above it still shows the jobs pages report.
export default function NotificationHost({ activity }: { activity?: ReactNode }) {
  const toasts = useToasts();
  return (
    <div className="notif-host">
      {/* Each toast rides in a grid-row wrapper (.toast-slot) whose row
          collapses 1fr → 0fr on the way out, so the cards below it GLIDE up
          instead of snapping the moment one is dismissed. The wrapper is what
          animates height; the card itself only fades and slides (shell.css). */}
      {toasts.map((t) => (
        <div key={t.id} className={"toast-slot" + (t.leaving ? " leaving" : "")}>
          <Toast
            msg={t.msg}
            tone={t.tone}
            action={t.action}
            leaving={t.leaving}
            onClose={() => dismissToast(t.id)}
          />
        </div>
      ))}
      {!IS_EMBED && (activity ?? <DownloadManager />)}
      {!IS_EMBED && <ServerStatusBanner />}
    </div>
  );
}
