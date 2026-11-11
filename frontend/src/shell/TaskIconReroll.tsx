// THE ICON AS A BUTTON, while a title is being edited (`task_icons_enabled`,
// owner 2026-11-10: "click edit title and click on icon should randomly
// change that"): wraps the head's StatusIcon and, only during the rename,
// makes it press for a new face — the server picks one no other task in the
// project wears (`rerollTaskIcon`) and the listing repaints it everywhere.
// Outside the rename, or with the experiment off, or on a task with no slot
// yet, the children render as they are: nothing to press, no extra box.
//
// `onMouseDown` is swallowed so the title input keeps the keyboard: its blur
// is what SAVES the rename and ends the edit, and a reroll must not end it.
import { useState, type ReactNode } from "react";
import { rerollTaskIcon, type Task } from "@platform/lib/api";
import { useTaskIcons } from "@platform/lib/task-icons-flag";
import { pokeTasks } from "./tasksPulse";

export function TaskIconReroll({
  task,
  editing,
  onDone,
  children,
}: {
  task: Pick<Task, "key" | "icon_slot">;
  /** True while the title input stands. */
  editing: boolean;
  /** After the server answered — a host that reloads its own facts. */
  onDone?: () => void;
  children: ReactNode;
}) {
  const iconsOn = useTaskIcons();
  const [busy, setBusy] = useState(false);
  if (!editing || !iconsOn || task.icon_slot === null || task.icon_slot === undefined) {
    return <>{children}</>;
  }
  return (
    <button
      type="button"
      className="task-icon-reroll"
      aria-label="New icon"
      title="New icon"
      disabled={busy}
      onMouseDown={(e) => e.preventDefault()}
      onClick={() => {
        if (busy) return;
        setBusy(true);
        rerollTaskIcon(task.key)
          .then(() => {
            pokeTasks();
            onDone?.();
          })
          .catch(() => {
            /* the face stays; the next press asks again */
          })
          .finally(() => setBusy(false));
      }}
    >
      {children}
    </button>
  );
}
