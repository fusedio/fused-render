// The Tasks pages' multi-select, as plain data (bulk archive). A selection is a
// Set of task keys; every edit returns a NEW set, or the SAME one when nothing
// changed, so a state setter fed the result is a no-op for an edit that did
// nothing. The hook that holds one per view mount is useTaskSelection.
//
// Nothing here reads the DOM or the clock.

import { isDraftTask, liveJobRow } from "@shell/tasks-lib";
import type { Task } from "@platform/lib/api";

/** Which tasks may be picked: any real task that is not already archived. */
export const selectableTask = (t: Task): boolean => t.status !== "archived" && !isDraftTask(t);

export type SelectionSet = ReadonlySet<string>;

export const EMPTY_SELECTION: SelectionSet = new Set<string>();

/** How long the one Undo toast for a bulk archive stays up. */
export const BULK_UNDO_MS = 8000;
/** Archives in flight at once: each one tears down runs and worktrees. */
export const BULK_CONCURRENCY = 4;

/** `sel` with `key` flipped. */
export function toggled(sel: SelectionSet, key: string): SelectionSet {
  const next = new Set(sel);
  if (!next.delete(key)) next.add(key);
  return next;
}

/** `sel` with every key of `keys` in (on) or out (off). */
export function withMany(sel: SelectionSet, keys: readonly string[], on: boolean): SelectionSet {
  let changed = false;
  const next = new Set(sel);
  for (const k of keys) {
    if (on ? !next.has(k) : next.delete(k)) {
      if (on) next.add(k);
      changed = true;
    }
  }
  return changed ? next : sel;
}

export type GroupState = "all" | "some" | "none";

/** A group's header checkbox: all of `keys` selected, some, or none. An empty
 *  group is "none". */
export function groupState(sel: SelectionSet, keys: readonly string[]): GroupState {
  if (keys.length === 0) return "none";
  let n = 0;
  for (const k of keys) if (sel.has(k)) n += 1;
  return n === 0 ? "none" : n === keys.length ? "all" : "some";
}

/** `sel` without the keys that are no longer in `visible`; the same set when
 *  none left. */
export function pruned(sel: SelectionSet, visible: Iterable<string>): SelectionSet {
  if (sel.size === 0) return sel;
  const live = new Set(visible);
  const keep = [...sel].filter((k) => live.has(k));
  return keep.length === sel.size ? sel : new Set(keep);
}

/** What the bulk bar's confirm says: how many tasks, and how many of them are
 *  running (a run in flight is stopped by archiving). */
export function archivePlan(
  tasks: readonly (ArchiveFacts & { key: string })[],
  keys: SelectionSet,
): { keys: string[]; running: number } {
  const picked = tasks.filter((t) => keys.has(t.key));
  return { keys: picked.map((t) => t.key), running: picked.filter(archiveNeedsConfirm).length };
}

/** The task fields `archiveNeedsConfirm` reads. */
export type ArchiveFacts = Pick<Task, "status" | "kind" | "background_jobs">;

/** Does archiving this task stop something? A run in flight is ended by the
 *  archive: running, waiting on a permission or question card (`needs_attention`),
 *  or a Done chat that still holds a live background job. ONE predicate for the
 *  bulk bar (`archivePlan`) and every single-task door (the bulk bar's `confirmText`). */
export const archiveNeedsConfirm = (task: ArchiveFacts): boolean =>
  task.status === "in_progress" || task.status === "needs_attention" || liveJobRow(task);

/** "Archive 5 tasks? 2 are still running and will be stopped." */
export function confirmText(count: number, running: number): string {
  const what = count === 1 ? "1 task" : `${count} tasks`;
  const run = running === 1 ? "1 is" : `${running} are`;
  return `Archive ${what}? ${run} still running and will be stopped.`;
}

export const archivedLabel = (n: number): string => `Archived ${n === 1 ? "1 task" : `${n} tasks`}`;

/** Run `fn` over `items` with at most `limit` in flight. Never rejects: each
 *  item's outcome is `{ok}` or `{error}`, in item order. */
export async function runPool<T>(
  items: readonly T[],
  limit: number,
  fn: (item: T) => Promise<unknown>,
): Promise<{ item: T; ok: boolean; error?: Error }[]> {
  const out: { item: T; ok: boolean; error?: Error }[] = new Array(items.length);
  let next = 0;
  const worker = async (): Promise<void> => {
    while (next < items.length) {
      const at = next++;
      try {
        await fn(items[at]);
        out[at] = { item: items[at], ok: true };
      } catch (e) {
        out[at] = { item: items[at], ok: false, error: e instanceof Error ? e : new Error(String(e)) };
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return out;
}
