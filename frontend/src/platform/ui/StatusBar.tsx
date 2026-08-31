// The bottom status bar (SPEC §36, D563, redesigned D565): a thin strip
// inside `#main` holding THREE ALWAYS-PRESENT categories — Models, Activity
// and Notifications — left to right, so a page's content ends above it
// instead of a floating card overlaying it.
//
// STATUS-BAR MERGE, THEN A PARTIAL REVERT: this bar used to hold four
// categories — Models, Engines, Jobs and Notifications (D591 added Engines;
// D579 renamed Jobs/Notifications from Activity/Updates). Models and Engines
// were their own PERSISTENT-status chips (what is resident/running right
// now), separate from Jobs and Notifications' TRANSIENT work that appears and
// resolves. User feedback was that four chips for what reads as one idea —
// "what is the machine doing" — was one too many, so Models and Engines both
// folded into the chip Jobs already owned, renamed `Activity`
// (`platform/ui/DownloadManager.tsx`). A follow-up revision then split Models
// back out into its own chip again (`shell/ModelsDock.tsx`, resurrected):
// the user relies on that chip's own filled/outlined `StatusDot` to know
// whether the machine is holding any model weights, and a dot shared with
// jobs and engines no longer answered that question on its own. Engines
// stayed folded into Activity — nothing comparable was ever asked of its own
// indicator. `models` is therefore back as a prop below, `engines` is DELETED
// along with `shell/EnginesDock.tsx`, `activity` still carries jobs + engines
// (`platform/ui/DownloadManager.tsx` — see its own header for the panel's two
// labelled sections), and `repoUpdates` (Notifications) is untouched.
//
// USER COMPLAINT THIS EXISTS TO FIX (round 1): "the collapsed notification is
// also taking too much space. lets have a bottom status bar where the
// notifications can be collapsed to. it is impossible to use the claude
// template with it." The two cards that used to live in NotificationHost's
// fixed, bottom-right column (`position: fixed`) overlaid whatever was under
// them REGARDLESS of collapse state — collapsing shrank a card's own
// footprint, it never gave the page back the space. This bar is the
// opposite: `#main` is already `flex: 1 1 auto; display: flex;
// flex-direction: column` (explorer.css), so the bar as its LAST child
// reserves height for free, and every page's content shortens by exactly the
// bar's height — nothing may overlap while a section is collapsed.
//
// ALWAYS PRESENT NOW (D565, user verdict on the shipped round-1 bar: "this is
// very ugly. different categories of status bar should be always present and
// look better"). `.status-bar:empty { display: none }` and the "empty means
// gone" reasoning D563 built are SUPERSEDED, not merely extended: a page with
// nothing loaded, nothing running and nothing behind used to render no bar at
// all, and the user's own words reject that — the categories are a
// fixed status readout, like a real status bar, not a notification stack that
// happens to sit at the bottom. Each section still draws its own IDLE state
// when it has nothing to say ("No models loaded" / "No activity" / "No
// notifications") — plain, muted text with no chevron, since there is no
// panel behind an idle section worth opening — rather than an empty box
// shouting for attention.
//
// COLLAPSED IS A CHIP, EXPANDED IS A PANEL, and all three sections work the
// same way: a `.dl-toggle` chip that opens a `.dl-panel` floating above it.
//
// LIFETIME ORDER, LEFT TO RIGHT: Models is PERSISTENT status, true the
// instant anything is resident — it does not "resolve" the way a download
// finishes — so it sits leftmost, the same principle D591 originally gave
// Models and Engines together. Activity and Notifications are TRANSIENT work
// that appears and resolves, the same lifetime-ordering principle
// NotificationHost.tsx documents for its own column (a toast is seconds,
// work in progress is minutes, the server card outlives the session) —
// applied here to what is always true versus what is currently happening,
// rather than to how long each lives.
//
// EVERY CHIP IS A LABEL PLUS ONE CIRCLE — outlined when the section holds
// nothing, filled when it holds anything (D588/D590, user: "no count. just a
// circle outlined or filled"). Nothing else is on a chip: the chevron went in
// D573, the aggregate percentage (`dl-pct`) in D581, Models' size readout in
// D589, the counts in D588/D590, and the "something unacknowledged" dot
// (`.dl-new-dot`) in D588 — the one circle answers "is there anything here",
// which is all a bar this thin has room to say. On Models that circle fills
// whenever ANY model is resident — the exact signal that earned this section
// its own chip back — and on Activity it answers a narrower question, "is
// there work right now": see DownloadManagerView's own header for why a
// running engine alone leaves ITS dot unfilled. `.is-idle` muting is the
// only other state a chip carries.
//
// THE FOLD IS NOT PERSISTED, AND NOT LIFTED HERE (D603). Every section starts
// collapsed on every load, unconditionally. Each section still OWNS its own
// in-session collapse state — this component does not lift it, it only hosts
// what each section renders. One panel is open at a time
// (`lib/exclusiveSection.ts`, D582). A panel opens on a USER action, or
// transiently for a job arrival in Activity, or a repo/failure arrival in
// Notifications (`lib/autoExpand.ts`); Models never auto-opens (D587,
// unaffected by any of this — see `shell/ModelsDock.tsx`).
//
// `models`/`activity`/`repoUpdates` are handed in rather than imported, for
// the exact layering reason NotificationHost.tsx's own header comment states
// for the entries this bar took over: a queue row has to offer "Open in
// Explorer", whose one answer lives in shell/schedule-lib; a repo row's
// refusal stages a prompt into explorer/lib's own store; Models needs
// apps/ai_models/lib's shared runtime poll; Activity's engine rows need
// platform/lib/api's engine poll — and platform may not import shell or apps
// (frontend/scripts/check-boundaries.mjs). So the shell composes all three
// (`shell/ModelsDock.tsx`, `shell/ActivityDock.tsx`,
// `shell/RepoUpdatesDock.tsx`) and this file keeps owning where they sit.
// Omitted, the bare download manager stands in `activity`'s place — same
// fallback NotificationHost used to provide, for the same reason: this
// component must not depend on a shell that may not be there.
//
// EMBEDS: top-level-document only, same guard `App.tsx` uses for the
// sidebar and used to use for these entries inside NotificationHost — a
// pane in panel/tab mode gets its own document and must not grow its own
// bar.
import type { ReactNode } from "react";
import DownloadManager from "@platform/ui/DownloadManager";

export default function StatusBar({
  models,
  activity,
  repoUpdates,
}: {
  models?: ReactNode;
  activity?: ReactNode;
  repoUpdates?: ReactNode;
}) {
  return (
    <div className="status-bar">
      {models}
      {activity ?? <DownloadManager />}
      {repoUpdates}
    </div>
  );
}
