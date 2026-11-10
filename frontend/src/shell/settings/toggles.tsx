// The one-switch settings: Chats & tasks, Sharing & sync, Experimental flags.
//
// Most of them publish after the PUT as well as saving: the surface the switch
// governs is usually already on screen (the sidebar beside this page, a grid
// in a split, an open composer) and reads the flag from its own module cache,
// so without the publish it would keep showing the old answer until the next
// navigation. Each flag module's header says why its reader caches.
import {
  putAppSharingEnabled,
  putCanvasesEmbedProxy,
  putCanvasesEnabled,
  putCanvasRailPeekEnabled,
  putTaskFavouritesEnabled,
  putTaskHeadUnifiedEnabled,
  putGitAutoSyncEnabled,
  putLivePreviewsEnabled,
  putMonitorEnabled,
  putProjectQueueEnabled,
  putReaderEnabled,
  putTaskNotifyTerminalSessionsEnabled,
  putChatRecapEnabled,
  putBreakoutEnabled,
  putTaskSummaryTitlesEnabled,
  putClaudeDefaultPermission,
  putClaudeStrictMcpEnabled,
  putContribRemixEnabled,
} from "@platform/lib/api";
import { publishContribFlags } from "@platform/lib/contrib-flags";
import { publishCanvasesEnabled } from "@apps/canvases/feature-flag";
import { publishTaskHeadUnified } from "../task-head-flag";
import { publishCanvasRailPeek } from "@shell/claude-rail/rail-peek-flag";
import { publishFavouritesParent, publishTaskFavourites } from "@platform/lib/favourites-flag";
import { publishAppSharingEnabled } from "@platform/lib/share-app-flag";
import { publishLivePreviewsEnabled } from "@platform/lib/live-previews-flag";
import { publishMonitorEnabled } from "@platform/lib/monitor-flag";
import {
  publishDefaultPermission,
  publishProjectQueueEnabled,
  publishChatRecapEnabled,
  publishBreakoutEnabled,
} from "@apps/claude/feature-flag";
import { publishTaskNotifyTerminalSessions } from "../task-notify-terminal-flag";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { PERMISSION_LABELS } from "@apps/claude/protocol/summaries";
import { PrefToggleRow, SettingRow, SettingSelect, usePrefWrite, type PrefControlProps } from "./SettingRow";

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

// Breakout side chats: research-only side chats seeded with messages from a conversation. Opt-in beta.
export function BreakoutControl({ prefs, onChange }: PrefControlProps) {
  return (
    <PrefToggleRow
      id="breakout"
      checked={prefs.breakout?.enabled === true}
      put={putBreakoutEnabled}
      after={(next) => publishBreakoutEnabled(next.breakout?.enabled === true)}
      onChange={onChange}
    />
  );
}

// The in-chat recap fold. Default on: one haiku call per recap.
export function ChatRecapControl({ prefs, onChange }: PrefControlProps) {
  return (
    <PrefToggleRow
      id="chat-recap"
      checked={prefs.chat_recap?.enabled !== false}
      put={putChatRecapEnabled}
      after={(next) => publishChatRecapEnabled(next.chat_recap?.enabled !== false)}
      onChange={onChange}
    />
  );
}

// Stable AI titles for tasks, refreshed after each turn. The server names the
// row itself, so no publish: the next poll carries the new title.
export function TaskSummaryTitlesControl({ prefs, onChange }: PrefControlProps) {
  return (
    <PrefToggleRow
      id="task-summary-titles"
      checked={prefs.task_summary_titles?.enabled === true}
      put={putTaskSummaryTitlesEnabled}
      onChange={onChange}
    />
  );
}

// Lean Claude sessions: no user MCP servers / connectors in chat and tasks.
// OFF unless stored true, so `=== true` (an older server has no field).
export function ClaudeStrictMcpControl({ prefs, onChange }: PrefControlProps) {
  return (
    <PrefToggleRow
      id="claude-strict-mcp"
      checked={prefs.claude_strict_mcp?.enabled === true}
      put={putClaudeStrictMcpEnabled}
      onChange={onChange}
    />
  );
}

// The Approvals mode a new chat opens on. A select, not a switch; published so
// a composer already on screen (and the next send) reads the new default.
// "auto" when unset (an older server has no field).
const PERMISSION_CHOICES = ["plan", "prompt", "acceptEdits", "auto"] as const;
export function ClaudeDefaultPermissionControl({ prefs, onChange }: PrefControlProps) {
  const { busy, error, run } = usePrefWrite(onChange);
  const current = prefs.claude_default_permission ?? "auto";
  return (
    <SettingRow
      id="claude-default-permission"
      control={
        <SettingSelect
          id="claude-default-permission"
          value={PERMISSION_CHOICES.includes(current as (typeof PERMISSION_CHOICES)[number]) ? current : "auto"}
          options={PERMISSION_CHOICES.map((key) => ({ key, label: PERMISSION_LABELS[key] }))}
          disabled={busy}
          onChange={(v) =>
            void run(
              () => putClaudeDefaultPermission(v),
              (next) => publishDefaultPermission(next.claude_default_permission),
            )
          }
        />
      }
    >
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </SettingRow>
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

// ── Sharing & sync ─────────────────────────────────────────────────────────

// Git auto-sync: default ON (`!== false` — an older server's silence is the
// default), an opt-OUT. Off is the Update card and the manual git flows only.
export function GitSyncControl({ prefs, onChange }: PrefControlProps) {
  return (
    <PrefToggleRow id="git-sync" checked={prefs.git?.auto_sync !== false} put={putGitAutoSyncEnabled} onChange={onChange} />
  );
}

// Public link (owner 2026-10-27): the Share sheet's Fused-account link card.
// The Share button is always there (share-app-flag.ts `useAppShareOffered`:
// the contribution loop is always on), the .fused file always. The GitHub
// install link rides on the loop — its own row is gone.
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

// ── Community (preview) ────────────────────────────────────────────────────
// Remix's switch. The contribution loop it rides on is always on since
// 2026-10-29 (its own switch — four phase switches until 2026-10-22 — is
// gone). Same publish-after-PUT as the rows above (contrib-flags.ts), so the
// cards and the share sheet already mounted follow.

export function ContribRemixControl({ prefs, onChange }: PrefControlProps) {
  return (
    <PrefToggleRow
      id="contrib-remix"
      checked={prefs.contrib?.remix === true}
      put={(next) => putContribRemixEnabled(next)}
      after={(next) => publishContribFlags(next.contrib)}
      onChange={onChange}
    />
  );
}

// ── Experimental flags ───────────────────────────────────────────────────────────────
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

// No publish: the proxy follows the pref on the next canvas status read, which
// every page that frames a canvas makes before it frames it. Off stops it at
// once (shell/prefs.py). The settings file can pin it, like the env var that
// pins the default environment.
export function CanvasesEmbedProxyControl({ prefs, onChange }: PrefControlProps) {
  const locked = prefs.canvases.embed_proxy_locked === true;
  const on = prefs.canvases.embed_proxy === true;
  return (
    <PrefToggleRow
      id="canvases-embed-proxy"
      checked={on}
      put={putCanvasesEmbedProxy}
      disabled={locked}
      status={
        locked ? (
          <>
            Pinned {on ? "on" : "off"} by <code>[render] canvases_embed_proxy_enabled</code> in{" "}
            <code>~/.fused/settings.toml</code>.
          </>
        ) : undefined
      }
      onChange={onChange}
    />
  );
}

// Published (rail-peek-flag.ts), so a page with a rail in THIS tab follows the
// switch at once. Another tab's shell holds its first answer until it reloads
// — the same deal every published flag here offers.
export function CanvasRailPeekControl({ prefs, onChange }: PrefControlProps) {
  const on = prefs.canvas_rail_peek?.enabled === true;
  return (
    <PrefToggleRow
      id="canvases-rail-peek"
      checked={on}
      put={putCanvasRailPeekEnabled}
      // …and to the favourites flag, which is on only under this one.
      after={(next) => {
        publishCanvasRailPeek(next.canvas_rail_peek);
        publishFavouritesParent(next.canvas_rail_peek);
      }}
      status={
        on
          ? "On — hovering a chat on the rail opens it as a card you can use; a click keeps it open."
          : "Off — hovering shows the chat's one-line caption; clicking opens it in the panel."
      }
      onChange={onChange}
    />
  );
}

// Published (favourites-flag.ts), like the rail preview above. A CHILD of that
// preview: the star lives on its card, so with the preview off every reader
// treats favourites as off whatever this says — the row reads inert then
// (greyed, switch disabled, the call-log children's idiom in advanced.tsx),
// still shown so the stored answer stays visible.
export function TaskFavouritesControl({ prefs, onChange }: PrefControlProps) {
  const on = prefs.task_favourites?.enabled === true;
  const parentOn = prefs.canvas_rail_peek?.enabled === true;
  return (
    <PrefToggleRow
      id="tasks-favourites"
      checked={on}
      put={putTaskFavouritesEnabled}
      after={(next) => publishTaskFavourites(next.task_favourites)}
      disabled={!parentOn}
      off={!parentOn}
      status={
        !parentOn
          ? "Needs Preview chats from the collapsed Claude rail."
          : on
            ? "On — a star on the preview card keeps a chat in a Favourites section at the rail's foot, its own card open beside it on every page."
            : "Off — no star, no Favourites section."
      }
      onChange={onChange}
    />
  );
}

export function TaskHeadUnifiedControl({ prefs, onChange }: PrefControlProps) {
  const on = prefs.task_head_unified?.enabled === true;
  return (
    <PrefToggleRow
      id="tasks-unified-head"
      checked={on}
      put={putTaskHeadUnifiedEnabled}
      after={(next) => publishTaskHeadUnified(next.task_head_unified)}
      status={
        on
          ? "On — every task view shows the same header: close or back, prev/next, status, star, number, title, Open and ⋮."
          : "Off — each view keeps its own header."
      }
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
