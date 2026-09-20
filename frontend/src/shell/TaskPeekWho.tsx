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
import { basename, firstLine, ringFailed, shortTaskId, taskColumn, tildePath } from "./tasks-lib";

/** The title the header prints — the task's own first line, and the word every
 *  surface uses for a task that has none. Exported because the peek's menu and
 *  its captions name the same string. */
export function peekTitle(task: Task): string {
  return firstLine(task.title) || "(untitled)";
}

/**
 * THE ONE LINE EVERY SURFACE NAMES A TASK BY — the list row, the chat header,
 * the peek panel, the cards popup (Akshil, 2026-09-15: "show the same title
 * everywhere"): the task's own title, and the card's "(untitled)" word for a
 * task that has none. Still a hook by name — until 2026-09-20 it subscribed to
 * the "title a task by your last message" pref, and every caller is a
 * component that calls it unconditionally. Takes `null` so a panel with no
 * task yet can still call it.
 */
export function useTaskHeadline(task: Task | null | undefined): string {
  if (!task) return "";
  return peekTitle(task);
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
export function TaskPeekWho({ task, running = false }: {
  task: Task;
  /**
   * THE PAGE KNOWS A TURN IS LIVE before the listing does. A chat sent from
   * this app runs `claude -p`, which the server's watcher learns about from
   * the transcript — a poll or two behind the send — so the ring read "done"
   * for the first seconds of every turn, and for a short turn for all of it
   * (Akshil, 2026-09-15). The controller's own clock outranks the row here;
   * the row catches up and agrees.
   */
  running?: boolean;
}) {
  // THE SAME LINE THE LIST ROW PRINTS. With "Title a task by your last message"
  // on, the row under this header shows the reader's newest message; a header still
  // showing the task's name was two surfaces naming one chat two ways
  // (Akshil, 2026-09-15: "header also shows last message").
  const title = useTaskHeadline(task);
  // …EXCEPT A ROW PARKED ON A CARD. `running` stays true while a permission
  // or plan card is open — the run clock has no waiting state — and the list
  // row says needs-attention for exactly that; the header must not spin over
  // a question the reader is being asked (StatusIcon's own note on `failed`).
  const column = taskColumn(task);
  const status = running && column !== "needs_attention" ? "in_progress" : column;
  return (
    <div className="task-side-peek-who">
      <span
        className="task-side-peek-status"
        title={status ? columnLabel(status) : undefined}
      >
        <StatusIcon status={status} failed={running ? false : ringFailed(task)} />
      </span>
      <span className="task-side-peek-id">{shortTaskId(task.task_id)}</span>
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
