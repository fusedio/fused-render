// Which task folders are GONE — the one fact the Tasks page cannot read off a
// task row and has to ask the disk about.
//
// A task's row outlives its folder: a run in a scratch directory that was later
// deleted still has its entry, its title, its status and its session id, and
// every view drew it as if it could be opened. Clicking it went to the Explorer,
// which answered with a raw stat error and no Claude pane (Akshil, 2026-09-06:
// "we have entries for them, but we don't have content … let's show clear error
// message in that case"). This hook is how the List and the Board learn that
// BEFORE the click, so the row can say so in its own words instead.
//
// One stat per DISTINCT folder, ever, for this mounting: the page polls every
// 20s and a wall of 500 tasks spans a few dozen folders, so the set of folders
// asked about is remembered in a ref and a poll adds nothing to it unless a new
// folder appears. Only a 404 counts as missing — a network blip or a 500 must
// not repaint every row on the page as "gone" for the twenty seconds until the
// next poll; those folders simply stay un-asked and are asked again next time.
import { startTransition, useEffect, useRef, useState } from "react";
import { statPath } from "@platform/lib/api";
import { getRetainedNotifications, notify } from "@platform/lib/notifications";
import type { HttpError, Task } from "@platform/lib/api";

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

/** How long a folder whose stat failed for a reason other than 404 waits before
 *  it is asked again — the page's own poll interval. */
const RETRY_MS = 20_000;

/** How long landed 404s are gathered before they are written as one set —
 *  long enough that a batch of stats answering together becomes a handful of
 *  renders, short enough that nobody notices the rows catching up. */
const FLUSH_MS = 300;

export function useMissingFolders(
  tasks: readonly Pick<Task, "target" | "project">[],
): ReadonlySet<string> {
  const [missing, setMissing] = useState<ReadonlySet<string>>(() => new Set());
  // Folders with a stat in flight or answered. A folder that answered 404 is in
  // `missing`; one that answered anything else is here and nowhere else — asked,
  // present, never asked again.
  const settled = useRef<Set<string>>(new Set());
  // The effect fires on the CONTENTS, not the array identity: `tasks` is a fresh
  // array every poll (TaskCards.useChatTemplates, same reason).
  const key = [...new Set(tasks.map(taskFolder).filter(Boolean))].sort().join("\u0000");
  // Alive until UNMOUNT, not until the next key: the effect re-runs whenever a
  // poll adds a folder, and a cleanup that cancelled the stats already in flight
  // would drop their answers while `settled` still said they had been asked —
  // a folder that 404'd during the page's first two polls would never be marked.
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  // A non-404 is not an answer, and the effect below runs on `key` — which a
  // poll that adds no folder leaves unchanged — so a blip would otherwise never
  // be re-asked (Bugbot, #1023). The catch bumps this after a poll's worth of
  // time, which re-runs the effect over the folders `settled` no longer holds.
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const asked: string[] = [];
    for (const dir of key.split("\u0000")) {
      if (!dir || settled.current.has(dir)) continue;
      settled.current.add(dir);
      asked.push(dir);
    }
    if (!asked.length) return;
    // ONE ANSWER FOR THE WHOLE BATCH, AS A TRANSITION — not one state write per
    // folder as its stat lands (2026-10-03, macOS 14 native windows). Every
    // write here re-renders the whole page: `missing` is a prop of every row,
    // and a row whose folder is gone changes SHAPE (role, tabindex, its action
    // strip), so the 404s used to arrive as a run of separate full-list renders
    // right after the page opened — on a 700-row list that was two seconds of
    // frozen pointer on Safari 17's engine, where a full style pass of this
    // page costs ~3× Chromium's. Gathering the batch makes it one render, and
    // `startTransition` lets React yield that render to hover, scroll and
    // typing instead of holding the main thread until it is done. The rows say
    // "Folder missing" a beat later than they could; nothing waits on them.
    //
    // FLUSHED EVERY `FLUSH_MS`, NOT ONCE AT THE END: `getJson` has no timeout,
    // and a stat of a mount-backed folder can take seconds cold — one slow
    // answer must not hold every other folder's "Folder missing" hostage. The
    // 404s that have landed are written together on a short timer, so a batch
    // of 434 stats answering over ~1 s becomes three or four renders, not 434
    // and not one that waits for the straggler.
    const gone: string[] = [];
    // ONE RETRY TIMER PER BATCH, ARMED ON THE FIRST BLIP — not when the batch
    // settles (Bugbot, #1384). `getJson` has no timeout, so a stat of a cold
    // mount can hang for as long as it likes; a retry that waited for
    // `allSettled` would wait behind it, and every other blipped folder with
    // it. Armed here, the retry fires a poll's time after the first blip no
    // matter what the straggler does; the effect it re-runs asks only the
    // folders `settled` no longer holds, so the hung one is left alone.
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let flush: ReturnType<typeof setTimeout> | null = null;
    const write = () => {
      flush = null;
      if (!alive.current || !gone.length) return;
      const batch = gone.splice(0);
      startTransition(() => {
        setMissing((cur) => {
          const next = batch.filter((d) => !cur.has(d));
          return next.length ? new Set([...cur, ...next]) : cur;
        });
      });
    };
    const settle = (dir: string, e: HttpError | undefined) => {
      if (!alive.current) return;
      if (e?.status === 404) {
        gone.push(dir);
        if (flush === null) flush = setTimeout(write, FLUSH_MS);
      } else {
        // Not an answer about the folder; ask again in a poll's time.
        settled.current.delete(dir);
        if (retryTimer === null) {
          retryTimer = setTimeout(() => {
            if (alive.current) setRetry((n) => n + 1);
          }, RETRY_MS);
        }
      }
    };
    void Promise.allSettled(
      asked.map((dir) =>
        statPath(dir).then(
          () => {
            /* present: nothing to record */
          },
          (e: HttpError) => settle(dir, e),
        ),
      ),
    ).then(() => {
      if (flush !== null) clearTimeout(flush);
      write();
    });
  }, [key, retry]);
  return missing;
}
