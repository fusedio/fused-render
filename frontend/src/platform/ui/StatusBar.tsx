// The bottom status bar (SPEC §36, D563, redesigned D565): a thin strip
// inside `#main` holding three ALWAYS-PRESENT categories — Models, Activity,
// Updates — left to right, so a page's content ends above it instead of a
// floating card overlaying it.
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
// all, and the user's own words reject that — the three categories are a
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
// COLLAPSED IS A CHIP, EXPANDED IS A PANEL, for the two sections that have
// rows to show (Activity, Updates — Models never expands past its own quick
// popover, see ModelsDock.tsx). Each section owns its own collapse state
// exactly as it did in the floating column (`fused-render:jobs-collapsed` /
// `fused-render:repo-updates-collapsed` / `fused-render:models-collapsed`,
// three independent keys) — this component does not lift that state, it
// only hosts what each section renders. Collapsed, a section renders a
// small `.dl-toggle` chip — the summary line, the aggregate percentage
// (jobs only), the chevron, a quiet dot for something unacknowledged
// (`lib/autoExpand.ts`) — nothing more fits a bar this thin, and a control
// on it would compete with the one line it has. Expanded, that SAME toggle
// stays and a panel (`.dl-panel`) opens above it — floating over page
// content, which is fine and expected: it is USER-initiated, never forced
// open by an arrival (code review finding #4 — `useAutoExpandOnNew` no
// longer does that; see its own doc).
//
// LIFETIME ORDER, LEFT TO RIGHT: Models is PERSISTENT status that is always
// true the instant anything is resident — it does not "resolve" the way a
// download finishes — so it sits leftmost, first. Activity and Updates are
// TRANSIENT work that appears and resolves, the same lifetime-ordering
// principle NotificationHost.tsx documents for its own column (a toast is
// seconds, work in progress is minutes, the server card outlives the
// session) — applied here to what is always true versus what is currently
// happening, rather than to how long each lives.
//
// `models`/`activity`/`repoUpdates` are handed in rather than imported, for
// the exact layering reason NotificationHost.tsx's own header comment states
// for the two entries this bar took over: a queue row has to offer "Open in
// Explorer", whose one answer lives in shell/schedule-lib; a repo row's
// refusal stages a prompt into explorer/lib's own store; Models needs
// apps/ai_models/lib's shared runtime poll — and platform may not import
// shell or apps (frontend/scripts/check-boundaries.mjs). So the shell
// composes all three (`shell/QueueDock.tsx`, `shell/RepoUpdatesDock.tsx`,
// `shell/ModelsDock.tsx`) and this file keeps owning where they sit.
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
