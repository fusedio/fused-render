// THE RING THAT FILES THE TASK — the shared task head's Archive / Unarchive,
// standing over the status ring and revealed when the head is hovered (Akshil,
// 2026-11-11: "hovering the head turns the ring into Archive, like a Tasks row
// does").
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
//   * A SPENT PRESS HANDS THE SLOT BACK: `onPress` lets the head raise its
//     `.is-refiled` flag, which keeps the ring (now drawing the new state) in
//     front until the pointer leaves the head (styles/task-head.css).
//
// Its own module rather than lifted out of the row, because the row's button is
// one branch of a component that owns a dozen other acts and a note line; the
// row's markup and classes are untouched.
import { useState } from "react";
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
  /** On the press itself: the head's spent-press flag. */
  onPress?: () => void;
}) {
  const file = filingIntent(task);
  const archiveAsk = useArchiveConfirm();
  const [acting, setActing] = useState(false);
  if (!file) return null;
  const doFile = async (kind: FiledKind) => {
    setActing(true);
    try {
      if (kind === "archive") await archiveTask(task.key);
      else await unarchiveTask(task.key);
      pokeTasks();
      onFiled?.(kind);
    } catch (e) {
      notify({ title: (e as Error).message || `Could not ${kind}`, tone: "error" });
    } finally {
      setActing(false);
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
          onPress?.();
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
