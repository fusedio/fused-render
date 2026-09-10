// The floating notification column: one fixed, bottom-right stack holding
// every transient toast (lib/toast), the FDA nudge and the server-health
// card. Mounted once by App, alongside `StatusBar` (platform/ui/StatusBar.tsx).
//
// It replaced three competing surfaces — a bottom-centre global toast stack, a
// per-pane toast each of Listing and Preview positioned and expired itself,
// and this bottom-right card — which between them meant the same "Path copied"
// appeared in two different places depending on which view raised it, and a
// toast could sit next to (or under) an unrelated card in the other corner.
// One stack, one set of stacking rules, no auto-dismiss — a toast stays until
// the ✕ or the code that raised it clears it (lib/toast).
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
// still floating over it. What stays here — toasts, the job pop-up,
// `ServerStatusBanner` — is either seconds-long or exceptional enough that
// overlaying the page is the right call for it: see `StatusBar`'s own header
// comment for the two long-lived cards' reasoning, which used to live here.
//
// THE JOB POP-UP (SPEC actionable-notifications) is the one entry here that
// draws the SAME card the long-lived Notifications panel does (`JobRow`,
// reused through `JobPopupCard`) rather than a `Toast` — the user's own
// clarification was "the UI should still be the same notification card". It
// qualifies for THIS column, not the panel's, for the same "seconds-long"
// reason every other entry here does: `JobPopupCard` clears itself in well
// under 3 seconds regardless of what its job's tier keeps in the panel.
//
// Panes keep their attribution for free: in panel/tab mode each pane is its
// own document, so a pane's toast renders in THAT pane's bottom-right corner,
// not the window's. Only the top-level document shows the server card and
// the job pop-up (an embed would otherwise pop the same job once per pane,
// all saying the same thing — `App.tsx`'s own `!IS_EMBED` guard around
// `ActivityDock` already keeps a pane from ever producing one, and the guard
// here is belt-and-suspenders against that changing out from under it).
import ServerStatusBanner from "@platform/ui/ServerStatusBanner";
import Toast from "@platform/ui/Toast";
import JobPopupCard from "@platform/ui/JobPopupCard";
import { dismissToast, useToasts } from "@platform/lib/toast";
import { IS_EMBED } from "@platform/lib/router";
import type { Job } from "@platform/lib/jobs";

export default function NotificationHost({
  jobPopup,
  onJobPopupGone,
}: {
  /** The one terminal job currently popped, or `null` for none — "latest
   *  wins" is enforced upstream (jobs.ts `popupTick`), so this is never more
   *  than one job. Optional: a caller with no job source (the onboarding
   *  wizard's own mount, App.tsx) simply never has one to pass. */
  jobPopup?: Job | null;
  onJobPopupGone?: () => void;
} = {}) {
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
      {/* `key={jobPopup.id}` gives every newly popped job — including a
          repeat of an id that already left once, which cannot happen since
          jobs.py mints a fresh id per run, but costs nothing to keep general
          for — a fresh `JobPopupCard` instance with its own countdown. */}
      {!IS_EMBED && jobPopup && (
        <JobPopupCard key={jobPopup.id} job={jobPopup} onGone={onJobPopupGone ?? (() => {})} />
      )}
      {!IS_EMBED && <ServerStatusBanner />}
    </div>
  );
}
