// The bottom status bar (SPEC §36, D563, redesigned D565): a thin strip
// inside `#main` holding four ALWAYS-PRESENT categories — Models, Engines,
// Jobs, Notifications — left to right, so a page's content ends above it
// instead of a floating card overlaying it. (Engines arrived in D591; the
// labels were `Activity` and `Updates` until D579 renamed them to `Jobs` and
// `Notifications` — the props are still spelled `activity`/`repoUpdates`,
// which is the only place the old names survive.)
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
// happens to sit at the bottom. `#main` is therefore permanently ~34px
// shorter now, on every page, which is the accepted cost of that call. Each
// section still draws its own IDLE state when it has nothing to say (`No
// models loaded` / `No jobs` / `No notifications`, D569/D579 — each names its own
// category rather than the bare adjective round 2 shipped, which the user
// could not place: "what is idle? what is up to date?") — plain, muted text
// with no chevron, since there is no panel behind an idle section worth
// opening — rather than an empty box shouting for attention.
//
// COLLAPSED IS A CHIP, EXPANDED IS A PANEL, and all four sections work the
// same way: a `.dl-toggle` chip that opens a `.dl-panel` floating above it.
//
// EVERY CHIP IS A LABEL PLUS ONE CIRCLE — outlined when the section holds
// nothing, filled when it holds anything (D588/D590, user: "no count. just a
// circle outlined or filled"). Nothing else is on a chip: the chevron went in
// D573, the aggregate percentage (`dl-pct`) in D581, Models' size readout in
// D589, the counts in D588/D590, and the "something unacknowledged" dot
// (`.dl-new-dot`) in D588 — the one circle answers "is there anything here",
// which is all a bar this thin has room to say, and a chip whose contents
// cannot change width cannot make its neighbours jump. `.is-idle` muting is
// the only other state a chip carries.
//
// THE FOLD IS NOT PERSISTED, AND NOT LIFTED HERE (D603). Every section starts
// collapsed on every load, unconditionally: there used to be four independent
// `fused-render:*-collapsed` keys, and all four are DELETED, because a
// `.dl-panel` is a popover and a popover that restores itself across reloads
// covers the page on every navigation. Each section still OWNS its own
// in-session collapse state — this component does not lift it, it only hosts
// what each section renders. One panel is open at a time
// (`lib/exclusiveSection.ts`, D582). A panel opens on a USER action, or
// transiently for an arrival in the two sections that allow it (Jobs,
// Notifications — Models and Engines pass `neverOpen`; `lib/autoExpand.ts`).
//
// LIFETIME ORDER, LEFT TO RIGHT: Models and Engines are PERSISTENT status,
// true the instant anything is resident or running — neither "resolves" the way
// a download finishes — so they sit leftmost. Jobs and Notifications are
// TRANSIENT work that appears and resolves, the same lifetime-ordering
// principle NotificationHost.tsx documents for its own column (a toast is
// seconds, work in progress is minutes, the server card outlives the
// session) — applied here to what is always true versus what is currently
// happening, rather than to how long each lives.
//
// `models`/`engines`/`activity`/`repoUpdates` are handed in rather than imported, for
// the exact layering reason NotificationHost.tsx's own header comment states
// for the two entries this bar took over: a queue row has to offer "Open in
// Explorer", whose one answer lives in shell/schedule-lib; a repo row's
// refusal stages a prompt into explorer/lib's own store; Models needs
// apps/ai_models/lib's shared runtime poll — and platform may not import
// shell or apps (frontend/scripts/check-boundaries.mjs). So the shell
// composes all four (`shell/QueueDock.tsx`, `shell/RepoUpdatesDock.tsx`,
// `shell/ModelsDock.tsx`, `shell/EnginesDock.tsx`) and this file keeps owning
// where they sit.
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
  engines,
  activity,
  repoUpdates,
}: {
  models?: ReactNode;
  /** The running engine daemons (D591). Between Models and Jobs because both
   *  it and Models report what is RUNNING RIGHT NOW, where Jobs and
   *  Notifications are transient work that appears and resolves — the lifetime
   *  ordering this file's own header documents. */
  engines?: ReactNode;
  activity?: ReactNode;
  repoUpdates?: ReactNode;
}) {
  return (
    <div className="status-bar">
      {models}
      {engines}
      {activity ?? <DownloadManager />}
      {repoUpdates}
    </div>
  );
}
