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
// EVERY CHIP IS ONE COMPONENT, `platform/ui/StatusChip.tsx` (statusbar
// redesign, 2026-09-02): a label, a numeral when there is more than one thing
// to count, and a 2px progress line along the chip's bottom edge while work
// runs. The circle (`StatusDot`, D588/D590) is GONE — the label itself now
// carries the state. Models reads "Models" muted / the one model's own name /
// "Models 2"; Activity reads "Activity" muted / the one job's verb ("Erasing",
// "Downloading") over its progress line / "Activity 2" over the mean progress;
// Notifications reads "Notifications" with a count from one, red when one of
// them is a failure. The count is one grey pill on every chip — the sidebar's
// own count style — and the progress line sits along the chip's TOP edge. User brief: "show minimal progress in status bar
// and then when we click open the full information … a number next to
// notifications and jobs … if there is only one model loaded we can just show
// the model name".
//
// HOVER PREVIEWS, CLICK PINS, NOTHING AUTO-OPENS (`platform/lib/statusChip.ts`).
// Hovering a chip opens its panel; leaving closes it; a click pins it until a
// second click, Escape or an outside click. One panel at a time
// (`lib/exclusiveSection.ts`, D582). The old arrival-driven auto-open
// (`lib/autoExpand.ts`, DELETED) is what kept landing a panel on top of the
// Claude composer's send button uninvited — the panel still floats above the
// bar, but now only while the pointer is on it or the user pinned it. The fold
// is not persisted (D603): every load starts closed.
//
// THE PANEL ANCHORS TO THE BAR'S RIGHT GUTTER, not to its own chip
// (`--status-bar-gutter` in notifications.css), so all three panels open at
// the same edge and the chips end where the header controls above them do.
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
