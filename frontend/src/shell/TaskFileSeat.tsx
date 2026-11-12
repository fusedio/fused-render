// THE RING THAT FILES THE TASK — the shared task head's Archive / Unarchive,
// standing over the status ring and revealed while the pointer is on the
// status icon itself (Akshil, 2026-11-12: "only when we hover over the task
// status" — the 2026-11-11 cut swapped on the whole head, which hid the status
// most of the time).
//
// THE LIST ROW'S GESTURE, NOT A NEW ONE. A Tasks row has done this since
// 2026-08-18 (`.tasks-rowmark` in ScheduleTaskViews, tasks.css "The mark slot"):
// one box, two occupants — the ring at rest, the filing button under the
// pointer — and the same rules hold here because it is the same reader asking
// the same question of the same task:
//
//   * ONE BUTTON, ONE DIRECTION: `filingIntent` answers archive, or unarchive
//     on an archived task, or NOTHING while a run is in flight — and nothing
//     means no button, so the ring stays under the pointer rather than leaving a
//     blank where the task's status was.
//   * THE SAME CALLS: the archive-confirm guard (a task parked on a question is
//     asked about first), `archiveTask` / `unarchiveTask`, and `pokeTasks` so
//     every view re-reads. Silent on success — the ring redrawing in its new
//     state is the receipt (performUnarchive's rule); a refusal is said, in a
//     notice, since a head has no note line under it the way a row does.
//   * A SPENT PRESS HANDS THE SLOT BACK: `onPress` — raised when the filing
//     STARTS, never for a cancelled confirm — lets the head raise its
//     `.is-refiled` flag, which keeps the ring (now drawing the new state) in
//     front until the pointer leaves the head (styles/task-head.css).
//   * ONE TASK PER REQUEST: a confirm is dropped when the head walks to
//     another task, and an answer that lands after the walk re-reads the
//     listing without moving the head that now shows someone else.
//
// Its own module rather than lifted out of the row, because the row's button is
// one branch of a component that owns a dozen other acts and a note line; the
// row's markup and classes are untouched.
import { useEffect, useRef, useState } from "react";
import { archiveTask, unarchiveTask, type Task } from "@platform/lib/api";
import { notify } from "@platform/lib/notifications";
import { useArchiveConfirm } from "./archive-confirm";
import { ICON_ARCHIVE, ICON_UNARCHIVE } from "./ScheduleTaskViews";
import { filingIntent } from "./tasks-lib";
import { pokeTasks } from "./tasksPulse";

export type FiledKind = "archive" | "unarchive";

export function TaskFileSeat({
  task,
  onFiled,
  onPress,
}: {
  task: Task;
  /** After the server took it. The host does what its ⋮ does on Archive
   *  (advance, go back, close); an Unarchive keeps the reader where they are. */
  onFiled?: (kind: FiledKind) => void;
  /** When the filing STARTS (after the confirm, if one was asked): the head's
   *  spent-press flag. Not on the press — a cancelled confirm spent nothing. */
  onPress?: () => void;
}) {
  const file = filingIntent(task);
  const archiveAsk = useArchiveConfirm();
  const [acting, setActing] = useState(false);
  // WHOSE SEAT THIS IS NOW. The side peek walks tasks under a standing head
  // (⌃⇧J/K, ↑↓), so the task a confirm or a request was made for may no longer
  // be the one on screen when it answers.
  const liveKey = useRef(task.key);
  liveKey.current = task.key;
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  // A CONFIRM IS ABOUT ONE TASK: a switch (or the seat going away) drops it
  // unanswered, so "Archive" can never land on the task walked to.
  const { cancel } = archiveAsk;
  useEffect(() => cancel, [task.key, cancel]);
  if (!file) return null;
  const doFile = async (kind: FiledKind) => {
    // The request belongs to the task it was made for.
    const owner = task.key;
    onPress?.();
    setActing(true);
    try {
      if (kind === "archive") await archiveTask(owner);
      else await unarchiveTask(owner);
      // Every view re-reads either way…
      pokeTasks();
      // …but the host's move (advance, go back, close) is only for a head
      // still showing that task: done after a walk to B, it would move B.
      if (alive.current && liveKey.current === owner) onFiled?.(kind);
    } catch (e) {
      notify({ title: (e as Error).message || `Could not ${kind}`, tone: "error" });
    } finally {
      if (alive.current) setActing(false);
    }
  };
  return (
    <>
      <button
        type="button"
        className={"task-head-file tasks-act--" + file.kind}
        data-hint={file.title}
        aria-label={file.kind === "archive" ? "Archive this task" : "Unarchive this task"}
        disabled={acting}
        onClick={(e) => {
          e.stopPropagation();
          if (file.kind === "archive") archiveAsk.guard(task, () => void doFile("archive"));
          else void doFile("unarchive");
        }}
      >
        {file.kind === "archive" ? ICON_ARCHIVE : ICON_UNARCHIVE}
      </button>
      {archiveAsk.node}
    </>
  );
}
