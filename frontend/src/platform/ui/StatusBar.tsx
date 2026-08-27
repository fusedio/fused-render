// The bottom status bar (SPEC §36, D562): a thin strip inside `#main` that
// RESERVES layout space for the two long-lived notification cards — the
// activity card (jobs and the scheduled queue) and the repo-updates card —
// so a page's content ends above it instead of a floating card overlaying it.
//
// USER COMPLAINT THIS EXISTS TO FIX: "the collapsed notification is also
// taking too much space. lets have a bottom status bar where the
// notifications can be collapsed to. it is impossible to use the claude
// template with it." Both cards used to live in NotificationHost's fixed,
// bottom-right column (`position: fixed`), which overlays whatever is under
// it REGARDLESS of collapse state — collapsing a card there only ever
// shrank its own footprint, it never gave the page back the space. This bar
// is the opposite: `#main` is already `flex: 1 1 auto; display: flex;
// flex-direction: column` (explorer.css), so the bar as its LAST child
// reserves height for free, and every page's content shortens by exactly
// the bar's height — nothing may overlap.
//
// INSIDE #main, NOT SPANNING THE APP: the sidebar keeps its own full height
// and its own bottom (GlobalSidebar's Preferences menu) — this bar only ever
// answers to the main column's own content, the same scope `#main` already
// has for everything else.
//
// COLLAPSED IS A CHIP, EXPANDED IS A PANEL. Each card owns its own collapse
// state exactly as it did in the floating column (`fused-render:
// jobs-collapsed` / `fused-render:repo-updates-collapsed`, two independent
// keys, `DownloadManager.tsx` / `RepoUpdatesDock.tsx`) — this component does
// not lift that state, it only hosts what each card renders. Collapsed, a
// card renders a small `.dl-toggle` chip — the summary line, the aggregate
// percentage, the chevron, nothing else (there is no room in a bar this
// thin for anything more, and putting a button on the chip would compete
// with the one line it has). Expanded, that SAME toggle stays, and a panel
// (`.dl-panel`) opens above it — floating over page content, which is fine
// and expected: it is user-initiated, unlike the old column's permanent
// overlay.
//
// EMPTY MEANS GONE, NOT A THIN EMPTY STRIP. `.status-bar:empty { display:
// none }` (notifications.css) is what makes that free: DownloadManagerView
// and RepoUpdatesCardView already return `null` when they have nothing to
// show, so a page with no jobs, no queue and no repo behind renders zero
// DOM children here and the selector matches — `#main` gets its full height
// back without this component having to know either card's own emptiness
// rule.
//
// `activity`/`repoUpdates` are handed in rather than imported, for the exact
// layering reason NotificationHost's own header comment states for the same
// two entries before this move: a queue row has to offer "Open in Explorer",
// the one answer to which lives in shell/schedule-lib, and platform may not
// import shell (frontend/scripts/check-boundaries.mjs); a repo row's refusal
// stages a prompt into explorer/lib's own store. So the shell composes both
// cards (`shell/QueueDock.tsx`, `shell/RepoUpdatesDock.tsx`) and this file
// keeps owning where they sit. Omitted, the bare download manager stands in
// `activity`'s place — same fallback NotificationHost used to provide, for
// the same reason: this component must not depend on a shell that may not
// be there.
//
// EMBEDS: top-level-document only, same guard `App.tsx` uses for the
// sidebar and used to use for these two cards inside NotificationHost — a
// pane in panel/tab mode gets its own document and must not grow its own
// bar.
import type { ReactNode } from "react";
import DownloadManager from "@platform/ui/DownloadManager";

export default function StatusBar({
  activity,
  repoUpdates,
}: {
  activity?: ReactNode;
  repoUpdates?: ReactNode;
}) {
  return (
    <div className="status-bar">
      {activity ?? <DownloadManager />}
      {repoUpdates}
    </div>
  );
}
