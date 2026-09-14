// WHO A TASK PANEL IS ABOUT — the status ring, TASK-nnn, the title, and the
// project this task runs in.
//
// It was written inside `shell/TaskPeek.tsx`'s header and lives here because a
// SECOND surface draws it now: the native chat's own top line, whenever the
// conversation on screen has a task row behind it (Akshil, 2026-09-14 — "reuse
// the task side-peek header identity block"). The chat used to print a ✻ Claude
// wordmark, the target's name and a session number, which are three facts about
// the tool and its file and none about the task — and a reader moving between
// the peek and the chat had two different headers for the same conversation.
//
// EXTRACTED, NOT COPIED, and the classes are the peek's own (`styles/
// task-peek.css`): the point is that the two headers cannot drift, and two
// stylesheets for one block is exactly how they would. `TaskPeek` renders these
// components unchanged, so its header is the same markup it was.
//
// A MODULE OF ITS OWN rather than an export from `TaskPeek.tsx`: that file
// hosts `@apps/claude`'s `ChatMount`, so a chat importing it would close an
// import cycle — and the boundary check (scripts/check-boundaries.mjs) opens a
// hole for named shell modules one file at a time for the same reason.
import type { Task } from "@platform/lib/api";
import { columnLabel } from "./schedule-lib";
import { StatusIcon } from "./ScheduleTaskViews";
import { basename, firstLine, ringFailed, taskColumn, tildePath } from "./tasks-lib";

/** The title the header prints — the task's own first line, and the word every
 *  surface uses for a task that has none. Exported because the peek's menu and
 *  its captions name the same string. */
export function peekTitle(task: Task): string {
  return firstLine(task.title) || "(untitled)";
}

/**
 * STATUS · NUMBER · TITLE, in one flexing box (`.task-side-peek-who`).
 *
 * The ring is the LIST's ring, the same component and the same vocabulary — a
 * reader who learned the mark in the list does not learn it twice. The word is
 * the ring's tooltip rather than ink: it is the one fact here that repeats on
 * every row of the list behind the panel.
 *
 * The title is the only part that may be cut short (task-peek.css): a clipped
 * sentence still reads, a clipped number is a different number.
 */
export function TaskPeekWho({ task }: { task: Task }) {
  const column = taskColumn(task);
  const title = peekTitle(task);
  return (
    <div className="task-side-peek-who">
      <span
        className="task-side-peek-status"
        title={column ? columnLabel(column) : undefined}
      >
        <StatusIcon status={taskColumn(task)} failed={ringFailed(task)} />
      </span>
      <span className="task-side-peek-id">{task.task_id}</span>
      <span className="task-side-peek-title" title={title}>
        {title}
      </span>
    </div>
  );
}

/**
 * THE PROJECT, AND IT IS A FACT (Akshil, 2026-09-14 — the peek's own note): the
 * label of the folder this task runs in, muted, with the full path in its
 * tooltip. Not a door — the header keeps exactly one way out, and it says its
 * act in words.
 *
 * The basename is what fits; two folders can share one, which is why the tooltip
 * spells the whole path. `home` only decides whether that path wears a `~`, so a
 * caller that does not know the home directory loses the tilde and nothing else.
 */
export function TaskPeekProject({ task, home = "" }: { task: Task; home?: string }) {
  return (
    <span className="task-side-peek-project" title={tildePath(task.project, home)}>
      {basename(task.project)}
    </span>
  );
}
