// The one-switch settings: Tasks, Apps & sharing, Optional features.
//
// Most of them publish after the PUT as well as saving: the surface the switch
// governs is usually already on screen (the sidebar beside this page, a grid
// in a split, an open composer) and reads the flag from its own module cache,
// so without the publish it would keep showing the old answer until the next
// navigation. Each flag module's header says why its reader caches.
import {
  putAppSharingEnabled,
  putCanvasesEnabled,
  putChatHeadsupEnabled,
  putGitAutoSyncEnabled,
  putGithubLinkEnabled,
  putLivePreviewsEnabled,
  putMarketplaceEnabled,
  putMonitorEnabled,
  putProjectQueueEnabled,
  putReaderEnabled,
  putTaskNotifyTerminalSessionsEnabled,
  putTaskRecapTitlesEnabled,
} from "@platform/lib/api";
import { publishCanvasesEnabled } from "@apps/canvases/feature-flag";
import { publishAppSharingEnabled } from "@platform/lib/share-app-flag";
import { publishLivePreviewsEnabled } from "@platform/lib/live-previews-flag";
import { publishMonitorEnabled } from "@platform/lib/monitor-flag";
import { publishMarketplaceEnabled } from "@platform/lib/marketplace-flag";
import { publishGithubLinkEnabled } from "@platform/lib/github-link-flag";
import {
  publishChatHeadsupEnabled,
  publishProjectQueueEnabled,
  publishTaskRecapTitlesEnabled,
} from "@apps/claude/feature-flag";
import { publishTaskNotifyTerminalSessions } from "../task-notify-terminal-flag";
import { PrefToggleRow, type PrefControlProps } from "./SettingRow";

// ── Tasks ──────────────────────────────────────────────────────────────────

// The project queue: one task at a time per folder, everything else waits.
// Opt-in while in beta. Its own row rather than a box inside a chat setting:
// the switch also governs Run now, the scheduler's dispatch and the Tasks
// board's Queued lane. `=== true` because a server that predates the field
// has no queue.
export function ProjectQueueControl({ prefs, onChange }: PrefControlProps) {
  const enabled = prefs.queue?.enabled === true;
  return (
    <PrefToggleRow
      id="project-queue"
      checked={enabled}
      put={() => putProjectQueueEnabled(!enabled)}
      // The chat's send path reads this from a module cache otherwise only
      // refreshed by a mount; a composer already on screen would keep
      // admitting (or not) by the old answer until a navigation.
      after={(next) => publishProjectQueueEnabled(next.queue?.enabled === true)}
      onChange={onChange}
    />
  );
}

// Haiku recaps titling the Tasks rows. Opt-in: one haiku call per turn.
export function TaskRecapsControl({ prefs, onChange }: PrefControlProps) {
  return (
    <PrefToggleRow
      id="task-recaps"
      checked={prefs.task_recap?.enabled === true}
      put={putTaskRecapTitlesEnabled}
      after={(next) => publishTaskRecapTitlesEnabled(next.task_recap?.enabled === true)}
      onChange={onChange}
    />
  );
}

// Heads-up notes: a side check may put one short note above the message box
// while Claude works. Opt-in: every check spends tokens from the user's plan.
// `=== true` because a server that predates the field has no notes.
export function ChatHeadsupControl({ prefs, onChange }: PrefControlProps) {
  return (
    <PrefToggleRow
      id="chat-headsup"
      checked={prefs.chat_headsup?.enabled === true}
      put={putChatHeadsupEnabled}
      after={(next) => publishChatHeadsupEnabled(next.chat_headsup?.enabled === true)}
      onChange={onChange}
    />
  );
}

// A Finished notification is scoped to sessions started from fused-render's
// own Claude template; this opts a hand-started terminal session back in.
// `?? false`: a server that predates the switch sends nothing, which is off.
export function NotifyTerminalControl({ prefs, onChange }: PrefControlProps) {
  return (
    <PrefToggleRow
      id="notify-terminal"
      checked={prefs.task_notify?.terminal_sessions ?? false}
      put={putTaskNotifyTerminalSessionsEnabled}
      // Published so useTaskStatusNotify picks it up on its next tick.
      after={(next) => publishTaskNotifyTerminalSessions(next.task_notify?.terminal_sessions === true)}
      onChange={onChange}
    />
  );
}

// ── Apps & sharing ─────────────────────────────────────────────────────────

// Git auto-sync: default ON (`!== false` — an older server's silence is the
// default), an opt-OUT. Off is the Update card and the manual git flows only.
export function GitSyncControl({ prefs, onChange }: PrefControlProps) {
  return (
    <PrefToggleRow id="git-sync" checked={prefs.git?.auto_sync !== false} put={putGitAutoSyncEnabled} onChange={onChange} />
  );
}

// The Share sheet (public link + .fused file) in place of the plain Export on
// every app surface — and on the file preview's share action too.
export function ShareSheetControl({ prefs, onChange }: PrefControlProps) {
  return (
    <PrefToggleRow
      id="share-sheet"
      checked={prefs.app_sharing.enabled}
      put={putAppSharingEnabled}
      after={(next) => publishAppSharingEnabled(next.app_sharing.enabled)}
      onChange={onChange}
    />
  );
}

// The Share sheet's GitHub install-link card. Only the sheet ever shows it, so
// it is a child of the row above and cannot be turned ON while that is off —
// but a link already on can always be turned off.
export function GithubLinkControl({ prefs, onChange }: PrefControlProps) {
  const sheet = prefs.app_sharing.enabled;
  const on = prefs.github_link?.enabled === true;
  return (
    <PrefToggleRow
      id="github-link"
      checked={on}
      put={putGithubLinkEnabled}
      after={(next) => publishGithubLinkEnabled(next.github_link?.enabled === true)}
      disabled={!sheet && !on}
      off={!sheet}
      status={sheet ? undefined : "Needs the Share sheet."}
      onChange={onChange}
    />
  );
}

// Live iframes in the /apps and explorer card thumbnails. OFF by default. The
// switch says it the positive way round now — on means live — where the old
// checkbox read "Hide live previews" and was checked when the pref was off.
export function LivePreviewsControl({ prefs, onChange }: PrefControlProps) {
  return (
    <PrefToggleRow
      id="live-previews"
      checked={prefs.live_previews?.enabled === true}
      put={putLivePreviewsEnabled}
      after={(next) => publishLivePreviewsEnabled(next.live_previews?.enabled === true)}
      onChange={onChange}
    />
  );
}

// ── Optional features ──────────────────────────────────────────────────────
// Each is off by default and this is the only place it turns on (Canvases:
// D427). Each one gates its ENTRY POINTS — a sidebar row, a status-bar chip, a
// file view — never the routes behind them.

export function CanvasesControl({ prefs, onChange }: PrefControlProps) {
  return (
    <PrefToggleRow
      id="canvases"
      checked={prefs.canvases.enabled}
      put={putCanvasesEnabled}
      // The sidebar beside this page reads the flag from its own store; the
      // row and the Settings-menu entry appear (or go) with the switch.
      after={(next) => publishCanvasesEnabled(next.canvases.enabled)}
      onChange={onChange}
    />
  );
}

export function MarketplaceControl({ prefs, onChange }: PrefControlProps) {
  return (
    <PrefToggleRow
      id="marketplace"
      checked={prefs.marketplace?.enabled === true}
      put={putMarketplaceEnabled}
      after={(next) => publishMarketplaceEnabled(next.marketplace?.enabled === true)}
      onChange={onChange}
    />
  );
}

export function MonitorControl({ prefs, onChange }: PrefControlProps) {
  return (
    <PrefToggleRow
      id="monitor"
      checked={prefs.monitor?.enabled === true}
      put={putMonitorEnabled}
      after={(next) => publishMonitorEnabled(next.monitor?.enabled === true)}
      onChange={onChange}
    />
  );
}

// Reader: the reader template's condition.py gate consults only this.
export function ReaderControl({ prefs, onChange }: PrefControlProps) {
  return <PrefToggleRow id="reader" checked={prefs.reader.enabled} put={putReaderEnabled} onChange={onChange} />;
}
