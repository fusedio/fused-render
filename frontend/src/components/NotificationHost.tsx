// The single notification surface: one fixed, bottom-right column holding
// every transient toast (lib/toast) with the server-health card pinned at its
// foot. Mounted once by App.
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
// Panes keep their attribution for free: in panel/tab mode each pane is its
// own document, so a pane's toast renders in THAT pane's bottom-right corner,
// not the window's. Only the top-level document shows the server card (an
// embed would otherwise render one per pane, all saying the same thing).
import Toast from "./Toast";
import ServerStatusBanner from "./ServerStatusBanner";
import { dismissToast, useToasts } from "../lib/toast";
import { IS_EMBED } from "../lib/router";

export default function NotificationHost() {
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
      {!IS_EMBED && <ServerStatusBanner />}
    </div>
  );
}
