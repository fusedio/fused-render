/**
 * THE TASK MENU'S VOCABULARY, ONE COPY (D1391).
 *
 * Two menus act on a task: the Tasks page peek's ⋯ (`shell/TaskPeek.tsx`) and
 * the chat panel's ⋮ (`apps/claude/ui/Kebab.tsx`). They grew apart — "Copy
 * command" beside "Copy terminal command", "Archive this task" beside
 * "Archive task", a refused Delete that said why in one and not the other —
 * so the words, the order and the "why is this greyed out" wording live here
 * and both menus read them. `platform/` because an app may not import from
 * `shell` (check:boundaries).
 *
 * THE RULES BOTH MENUS FOLLOW:
 *   * Order: the surface's own door (Full screen / Open as page), then the
 *     hand-offs (terminal, copy, download), then filing (archive, delete),
 *     a separator between groups.
 *   * HIDDEN when the action cannot apply to this task at all (no session yet:
 *     there is no command to hand over and no transcript to save).
 *   * DISABLED when it applies but is refused for now (a run in flight, a mode
 *     holding the page), and the label says why — a menu row's tooltip is
 *     not something a reader finds.
 */

export const TASK_ACTION_LABELS = {
  copyCommand: "Copy terminal command",
  download: "Download transcript",
  archive: "Archive task",
  unarchive: "Unarchive task",
  delete: "Delete task",
  fullScreen: "Full screen",
  exitFullScreen: "Exit full screen",
} as const;

/** The refusal a running task's Archive/Delete wear, in the words the server
 *  refuses in (`/api/tasks/erase`). */
export const RUN_BLOCKED_HINT = "Stop the run first";

/** The terminal hand-off's words. `agent` is the agent's display name once
 *  there is more than one to name (D18), else null; `hasSession` picks
 *  "continue" (a `--resume`) over "new session" (the folder only). */
export function terminalActionLabel(agent: string | null, hasSession: boolean): string {
  if (agent) return hasSession ? `Continue with ${agent} in terminal` : `New ${agent} session in terminal`;
  return hasSession ? "Continue in terminal" : "New session in terminal";
}

/** A refused row's label: `Delete task — stop the run first`. No reason, no
 *  suffix. */
export function blockedActionLabel(label: string, reason?: string | null): string {
  if (!reason) return label;
  return `${label} — ${reason.charAt(0).toLowerCase()}${reason.slice(1)}`;
}
