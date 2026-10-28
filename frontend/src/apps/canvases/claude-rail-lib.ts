// WHICH CHATS THE CLAUDE RAIL DRAWS, AND IN WHAT ORDER (ClaudeRail.tsx). Pure,
// so the two rulings below are testable without a DOM:
//
//   - THIS CANVAS'S CHATS, minus Archive (owner, 2026-10-28: "let's not show
//     archived tasks there"). A row that predates `task.canvas` is matched by
//     its project (the clone dir). Archive is read off the PULSE's status —
//     fresher than the listing — through the same `taskColumn` the Tasks page
//     buckets by, so a Cancelled or Skipped row leaves the rail the moment it
//     leaves the Board's live lanes.
//   - THE LIST VIEW'S ORDER (owner, 2026-10-28: "the order/sort there should
//     be similar to what we have in list view"): `sortForList`, the one
//     ordering function every Tasks view reads (tasks-lib, design.md §5), over
//     the listing's full rows with the pulse's status laid on top. The rail
//     used to sort newest-first by `last_active` on its own, which put a Done
//     chat above a running one whenever it had been touched more recently —
//     the exact drift that single function exists to end.
//
// A pulse row the listing does not hold yet (a chat made seconds ago, the
// listing's re-read still 600ms out) has no full row to rank: it leads the
// rail, newest first, which is where the List would put it too — a fresh In
// Progress row heads its lane, and In Progress is the first lane a session
// can be in.
import type { Task, TaskPulseTask } from "@platform/lib/api";
import { sortForList, taskColumn } from "@shell/tasks-lib";
import type { WorkbenchEnv } from "./api";

/** One rail seat: the pulse row (live status) and, once the listing holds it,
 *  the full row the caption is built from. */
export interface RailRow {
  pulse: TaskPulseTask;
  row: Task | null;
}

/** Where the rail stands: the canvas, and its clone dir once it exists. */
export interface RailPlace {
  env: WorkbenchEnv;
  name: string;
  dir: string | null;
}

/** A row's time (`last_active`, a number that climbs every time a run says
 *  anything), newest first. A row without one sorts last. (The rail's old
 *  sort parsed this as a date string, which a number never is, so every row
 *  read as 0 and "newest first" was the server's order wearing a label.) */
export function when(t: Pick<TaskPulseTask, "last_active">): number {
  return typeof t.last_active === "number" && Number.isFinite(t.last_active) ? t.last_active : 0;
}

/** Is this pulse row one of the rail's: a chat (it has a session) on THIS
 *  canvas, and not archived. */
export function onRail(t: TaskPulseTask, place: RailPlace): boolean {
  if (!t.session_id) return false;
  if (taskColumn(t) === "archived") return false;
  if (t.canvas && t.canvas.env === place.env && t.canvas.name === place.name) return true;
  return place.dir !== null && t.project === place.dir;
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
  const known: Task[] = [];
  const fresh: TaskPulseTask[] = [];
  for (const t of mine) {
    const row = listing.get(t.key);
    // The live status is the pulse's (fresher than the listing).
    if (row) known.push({ ...row, status: t.status });
    else fresh.push(t);
  }
  const byKey = new Map(mine.map((t) => [t.key, t]));
  const ordered = sortForList(known, now)
    .map((row): RailRow | null => {
      const pulse = byKey.get(row.key);
      return pulse ? { pulse, row } : null;
    })
    .filter((r): r is RailRow => r !== null);
  const head = fresh
    .slice()
    .sort((a, b) => when(b) - when(a))
    .map((pulse): RailRow => ({ pulse, row: null }));
  return [...head, ...ordered];
}
