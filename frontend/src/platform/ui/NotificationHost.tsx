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
// `repoUpdates` is a SEPARATE entry (SPEC §36), between `activity` and
// `<FdaCard />` — a repo behind its remote's default branch used to be rows
// pinned inside the activity card, exempt from that card's own header, fold
// and Clear, which broke all three at once (see RepoUpdatesDock.tsx's own
// module comment for the full history). It gets its own place in the
// lifetime order instead of merging back into `activity`: it outlives a job
// (a "your branch is behind" fact does not resolve itself the way a
// download finishes) but not the FDA nudge or the server card, both of
// which are near-permanent fixtures of the corner rather than something
// tied to a repo the user happens to have open right now.
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
import FdaCard from "@platform/ui/FdaCard";
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
// above it still shows the jobs pages report — INCLUDING a live scheduled run, which
// is the case a bare mount used to lose. The card dropped every running scheduled job
// on the assumption that a queue row above was drawing it, and with no queue slot
// filled there was none, so the run had no row and no stop. It is told which runs the
// slot covers now (DownloadManager's `QueueSlot.drawn`), and told nothing means it
// draws them itself.
export default function NotificationHost({
  activity,
  repoUpdates,
}: {
  activity?: ReactNode;
  repoUpdates?: ReactNode;
}) {
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
      {/* Its own sibling card (SPEC §36) — see the header comment for why it
          sits here rather than folding back into `activity`. Omitted like
          `activity` when the shell has nothing to hand in. */}
      {!IS_EMBED && repoUpdates}
      {/* Full Disk Access nudge (macOS packaged app only): longer-lived than a
          toast or a job — once granted or dismissed it never comes back — so it
          sits just above the one entry that outlives the session. */}
      {!IS_EMBED && <FdaCard />}
      {!IS_EMBED && <ServerStatusBanner />}
    </div>
  );
}
