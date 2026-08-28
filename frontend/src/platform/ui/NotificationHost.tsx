// The floating notification column: one fixed, bottom-right stack holding
// every transient toast (lib/toast), the FDA nudge and the server-health
// card. Mounted once by App, alongside `StatusBar` (platform/ui/StatusBar.tsx).
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
// TWO ENTRIES USED TO LIVE HERE AND DO NOT ANY MORE (D563, status bar
// redesign, user call: "the collapsed notification is also taking too much
// space... it is impossible to use the claude template with it"): the
// activity card (SPEC §36, work in progress — jobs and the scheduled queue)
// and the repo-updates card (SPEC §36, a repo behind its remote's default
// branch). Both are LONG-LIVED — minutes to indefinitely — and this column is
// FIXED, so even collapsed their header sat on top of whatever page was under
// it. `StatusBar` hosts them now, inside `#main`, where collapsing them
// actually gives the page its space back rather than merely shrinking a card
// still floating over it. What stays here — toasts, `FdaCard`,
// `ServerStatusBanner` — is either seconds-long or exceptional enough that
// overlaying the page is the right call for it: see `StatusBar`'s own header
// comment for the two long-lived cards' reasoning, which used to live here.
//
// Panes keep their attribution for free: in panel/tab mode each pane is its
// own document, so a pane's toast renders in THAT pane's bottom-right corner,
// not the window's. Only the top-level document shows the server card (an
// embed would otherwise render one per pane, all saying the same thing).
import FdaCard from "@platform/ui/FdaCard";
import ServerStatusBanner from "@platform/ui/ServerStatusBanner";
import Toast from "@platform/ui/Toast";
import { dismissToast, useToasts } from "@platform/lib/toast";
import { IS_EMBED } from "@platform/lib/router";

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
      {/* Full Disk Access nudge (macOS packaged app only): longer-lived than a
          toast — once granted or dismissed it never comes back — so it sits
          just above the one entry that outlives the session. */}
      {!IS_EMBED && <FdaCard />}
      {!IS_EMBED && <ServerStatusBanner />}
    </div>
  );
}
