// Which task folders are GONE — the one fact the Tasks page cannot read off a
// task row's PATHS and has to know about the disk.
//
// A task's row outlives its folder: a run in a scratch directory that was later
// deleted still has its entry, its title, its status and its session id, and
// every view drew it as if it could be opened. Clicking it went to the Explorer,
// which answered with a raw stat error and no Claude pane (Akshil, 2026-09-06:
// "we have entries for them, but we don't have content … let's show clear error
// message in that case"). This is how the List and the Board learn that BEFORE
// the click, so the row can say so in its own words instead.
//
// ANSWERED BY THE ROW, NOT BY A STAT (2026-10-09, Tasks latency design D4).
// This file used to be a hook that asked `/api/fs/stat` once per distinct
// folder — 516 requests per window on the owner's machine, 2 KB each, for a
// yes/no the server had already looked up while building the listing
// (`_task_entry` stats every target). The row now carries `folder_missing`,
// and what is left here is the pure projection of the rows onto the set the
// views read, plus the words the row and the toast say.
import { getRetainedNotifications, notify } from "@platform/lib/notifications";
import type { Task } from "@platform/lib/api";

/** The folder a task's conversation lives in — the same field, in the same
 *  order, as the folder door this page opens (schedule-lib.folderHref).
 *
 *  `project` FIRST, because this asks "is the FOLDER still there": a task made
 *  from inside an app targets the app's entry page, and target-first stat'd
 *  that FILE — so renaming index.html while the folder sat right there marked
 *  every one of the folder's tasks "Folder missing", disabled their Explorer
 *  door and toasted "This task's folder was deleted". It also split the
 *  one-stat-per-folder dedup below into one stat per entry file. `target` stays
 *  the fallback for a row carrying no project.
 *
 *  `tasks-lib.taskHref` deliberately still reads `target` first — it wants the
 *  file, and opens it with `_file=`. This one wants the place. */
export function taskFolder(task: Pick<Task, "target" | "project">): string {
  return task.project || task.target || "";
}

/** The hover caption on the "Folder missing" mark: the one place the path is
 *  worth printing, in the tilde form the folder chip beside it already uses. */
export function missingFolderHint(folder: string): string {
  return `Folder no longer exists — ${folder}`;
}

/** What a press on such a row or card says, as a TOAST and not a line under the
 *  row (Akshil, 2026-09-06: "instead of showing error message like this just
 *  show a error toast in simple user understandable language"). No path — the
 *  mark's hint carries it — and one plain instruction. */
export const MISSING_FOLDER_TOAST =
  "This task's folder was deleted, so its chat can't be opened. Archive the task to remove it.";

export function toastMissingFolder(): void {
  // One at a time — but the thing worth deduplicating against changed with
  // the move to notifications.ts: the old toast queue could hold up to
  // MAX_TOASTS live copies, so this checked the queue. The new store's
  // popup is "latest wins, one at a time" by construction (notify() always
  // replaces it), so a second press can no longer stack a second POPUP —
  // but tone: "error" retains an attention row in the panel on every call,
  // and three quick presses would otherwise leave three identical rows
  // sitting in "Needs you" forever (nothing there auto-expires). Dedup
  // against the RETAINED list instead of the popup for that reason.
  if (getRetainedNotifications().some((n) => n.title === MISSING_FOLDER_TOAST)) return;
  notify({ title: MISSING_FOLDER_TOAST, tone: "error" });
}

/** The folders of the given rows that the server says are gone — keyed the way
 *  the views ask (`missing.has(taskFolder(task))`). A new Set per call; the
 *  caller memoises on the rows. */
export function missingFolders(
  tasks: readonly Pick<Task, "target" | "project" | "folder_missing">[],
): ReadonlySet<string> {
  const gone = new Set<string>();
  for (const task of tasks) {
    if (!task.folder_missing) continue;
    const folder = taskFolder(task);
    if (folder) gone.add(folder);
  }
  return gone;
}
