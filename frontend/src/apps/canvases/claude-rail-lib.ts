// WHICH CHATS THE CLAUDE RAIL DRAWS, AND IN WHAT ORDER (ClaudeRail.tsx). Pure,
// so the two rulings below are testable without a DOM:
//
//   - THIS CANVAS'S CHATS, minus Archive (owner, 2026-10-28: "let's not show
//     archived tasks there"). A row that predates `task.canvas` is matched by
//     its project (the clone dir). Archive is read off the PULSE's status —
//     fresher than the listing — through the same `taskColumn` the Tasks page
//     buckets by, so a Cancelled or Skipped row leaves the rail the moment it
//     leaves the Board's live lanes.
//   - THE SELECTED NODE'S CHATS ONLY, while a node is selected (owner,
//     2026-10-29: "collapsed sidebar should behave exact same way as expanded
//     sidebar"): the open pane shows the node's own chat then, and its Recent
//     list holds that node's chats alone, so the rail narrows the same way. A
//     node chat is one whose target is the node's file (`task.canvas.file`,
//     routers/tasks.py `_task_canvas`); with no node selected the rail holds
//     every chat on the canvas, as before.
//   - THE LIST VIEW'S ORDER (owner, 2026-10-28: "the order/sort there should
//     be similar to what we have in list view"): `sortForList`, the one
//     ordering function every Tasks view reads (tasks-lib, design.md §5), over
//     the listing's full rows with the pulse's status laid on top. The rail
//     used to sort newest-first by `last_active` on its own, which put a Done
//     chat above a running one whenever it had been touched more recently —
//     the exact drift that single function exists to end.
//
// A pulse row the listing does not hold yet (a chat made seconds ago, the
// listing's re-read still 600ms out, or a re-read that failed) is ranked
// IN ITS LANE like every other: the pulse row carries every field the order
// reads (status, the background jobs, the run's times), so it sorts beside
// the listed rows rather than above them — a chat that just finished takes
// its place under the running ones, not the rail's head.
import type { Task, TaskPulseTask } from "@platform/lib/api";
import { sortForList, taskColumn } from "@shell/tasks-lib";
import type { WorkbenchEnv } from "./api";

/** One rail seat: the pulse row (live status) and, once the listing holds it,
 *  the full row the caption is built from. */
export interface RailRow {
  pulse: TaskPulseTask;
  row: Task | null;
}

/** Where the rail stands: the canvas, its clone dir once it exists, and the
 *  selected node's file (basename, as `task.canvas.file` names it) — null or
 *  absent for the canvas as a whole. */
export interface RailPlace {
  env: WorkbenchEnv;
  name: string;
  dir: string | null;
  file?: string | null;
}

/** Is this pulse row one of the rail's: a chat (it has a session) on THIS
 *  canvas — on the selected node, when one is — and not archived. */
export function onRail(t: TaskPulseTask, place: RailPlace): boolean {
  if (!t.session_id) return false;
  if (taskColumn(t) === "archived") return false;
  const named = !!t.canvas && t.canvas.env === place.env && t.canvas.name === place.name;
  if (!named && !(place.dir !== null && t.project === place.dir)) return false;
  if (!place.file) return true;
  // A row the server stamped names its node; one that predates `task.canvas`
  // is matched by its target, the node's file under the clone.
  if (named) return t.canvas!.file === place.file;
  return place.dir !== null && t.target === `${place.dir.replace(/\/+$/, "")}/${place.file}`;
}

/**
 * The rail's seats, top to bottom.
 *
 * `now` is read once by the caller and handed down, as `sortForList` asks:
 * every row is placed against one instant.
 */
export function railRows(
  rows: readonly TaskPulseTask[],
  listing: ReadonlyMap<string, Task>,
  place: RailPlace,
  now: number = Date.now(),
): RailRow[] {
  const mine = rows.filter((t) => onRail(t, place));
  const byKey = new Map(mine.map((t) => [t.key, t]));
  const held = new Map<string, Task>();
  const ranked: Task[] = mine.map((t) => {
    const row = listing.get(t.key);
    if (row) {
      held.set(t.key, row);
      // The live status is the pulse's (fresher than the listing).
      return { ...row, status: t.status };
    }
    // Not in the listing yet: the pulse row stands in. It is a `Pick` of the
    // Task — every field `sortForList` ranks by is in the pick (status, kind,
    // background_jobs, last_active, happened_at, next_run) and the fields it
    // lacks (draft, form, entry) read as absent, which is what a row with no
    // unsent words and no app page is.
    return t as Task;
  });
  return sortForList(ranked, now)
    .map((row): RailRow | null => {
      const pulse = byKey.get(row.key);
      return pulse ? { pulse, row: held.get(row.key) ?? null } : null;
    })
    .filter((r): r is RailRow => r !== null);
}
