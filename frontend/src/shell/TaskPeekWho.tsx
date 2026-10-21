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
import { useRef, useState } from "react";
import { Pencil } from "lucide-react";
import { renameTask, type Task } from "@platform/lib/api";
import { agentLabel, agentNamed, modelLabelsOf, useAgentBackends } from "@platform/lib/agent-backends";
import { columnLabel } from "./schedule-lib";
import { pokeTasks } from "./tasksPulse";
import { StatusIcon, TaskAgentMark } from "./ScheduleTaskViews";
import {
  BACKGROUND_LABEL,
  basename,
  cardTitleLine,
  firstLine,
  liveJobRow,
  ringFailed,
  shortTaskId,
  taskColumn,
  tildePath,
} from "./tasks-lib";

/** The title the header prints — the task's own first line, and the word every
 *  surface uses for a task that has none. Exported because the peek's menu and
 *  its captions name the same string. */
export function peekTitle(task: Task): string {
  return firstLine(task.title) || "(untitled)";
}

/**
 * THE ONE LINE EVERY SURFACE NAMES A TASK BY — the list row, the chat header,
 * the peek panel, the cards popup (Akshil, 2026-09-15: "show the same title
 * everywhere"). `cardTitleLine`'s rule: the reader's newest message, else the
 * task's own title, and the card's "(untitled)" word for a task that has
 * neither. Still a hook by name — until 2026-09-20 it subscribed to the "title
 * a task by your last message" pref, and every caller is a component that
 * calls it unconditionally. Takes `null` so a panel with no task yet can still
 * call it.
 */
export function useTaskHeadline(task: Task | null | undefined): string {
  if (!task) return "";
  return cardTitleLine(task).text || peekTitle(task);
}

/** A task the server can rename: a started session. `pending:` rows (a message
 *  not sent yet), `draft:` rows and `new:` chats have no session to hold a
 *  title. */
function renamable(task: Task): boolean {
  const key = task.key ?? "";
  return !!key && !/^(pending|draft|new):/.test(key);
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
 *
 * THE TITLE IS THE READER'S TO RENAME: double-click it (or the pencil that
 * shows on hover) and it becomes an input; Enter or blur saves, Escape
 * cancels, saving what is already there does nothing, and saving "" gives the
 * automatic title back. Same draft / busy / error shape as the Current apps
 * rename. `rename` is injectable so a test needs no network.
 */
export function TaskPeekWho({ task, running = false, rename = renameTask }: {
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
  rename?: (key: string, title: string) => Promise<unknown>;
}) {
  // THE SAME LINE THE LIST ROW PRINTS. With "Title a task by your last message"
  // on, the row under this header shows the reader's newest message; a header still
  // showing the task's name was two surfaces naming one chat two ways
  // (Akshil, 2026-09-15: "header also shows last message").
  const title = useTaskHeadline(task);
  const canRename = renamable(task);
  const [draft, setDraft] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // Enter saves and the unmount-time blur must not save twice; Escape must not
  // be undone by the blur it causes.
  const settled = useRef(false);
  const startRename = () => {
    if (!canRename || busy) return;
    settled.current = false;
    setError("");
    setDraft(title === "(untitled)" ? "" : title);
  };
  const cancelRename = () => {
    settled.current = true;
    setDraft(null);
    setError("");
  };
  const saveRename = async () => {
    if (draft === null || settled.current || busy) return;
    const next = draft.trim();
    // Compare with the STORED name, not the headline: in last-message or recap
    // mode the headline is not the task's name, and saving it unchanged is how
    // the reader pins it. Only an already-renamed task has a name to match.
    const stored = task.title_source === "renamed" ? (task.title || "").split("\n")[0].trim() : "";
    if (next === stored) return cancelRename();
    setBusy(true);
    try {
      await rename(task.key, next);
      settled.current = true;
      setDraft(null);
      setError("");
      pokeTasks();
    } catch (e) {
      setError((e as Error).message || "Could not rename");
    } finally {
      setBusy(false);
    }
  };
  // …EXCEPT A ROW PARKED ON A CARD. `running` stays true while a permission
  // or plan card is open — the run clock has no waiting state — and the list
  // row says needs-attention for exactly that; the header must not spin over
  // a question the reader is being asked (StatusIcon's own note on `failed`).
  const column = taskColumn(task);
  const status = running && column !== "needs_attention" ? "in_progress" : column;
  // …and a finished turn whose chat still holds a live background job wears the
  // List row's blue "Background" ring (tasks-lib.liveJobRow).
  const background = !running && liveJobRow(task);
  return (
    <div className="task-side-peek-who">
      <span
        className="task-side-peek-status"
        title={background ? BACKGROUND_LABEL : status ? columnLabel(status) : undefined}
      >
        <StatusIcon
          status={status}
          failed={running ? false : ringFailed(task)}
          background={background}
        />
      </span>
      <TaskAgentMark backend={task.backend} />
      <span className="task-side-peek-id">{shortTaskId(task.task_id)}</span>
      {draft === null ? (
        <>
          <span
            className="task-side-peek-title"
            title={title}
            onDoubleClick={canRename ? startRename : undefined}
          >
            {title}
          </span>
          {canRename && (
            <button
              type="button"
              className="icon-btn task-side-peek-rename"
              aria-label="Rename task"
              title="Rename task"
              onClick={startRename}
            >
              <Pencil size={12} aria-hidden />
            </button>
          )}
        </>
      ) : (
        <>
          <input
            className="task-side-peek-title-input"
            value={draft}
            disabled={busy}
            autoFocus
            aria-label="Task title"
            aria-invalid={error ? true : undefined}
            title={error || undefined}
            onFocus={(e) => e.currentTarget?.select?.()}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={() => void saveRename()}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                void saveRename();
              } else if (e.key === "Escape") {
                e.preventDefault();
                cancelRename();
              }
            }}
          />
          {error && <span className="task-side-peek-rename-error">{error}</span>}
        </>
      )}
    </div>
  );
}

/**
 * WHO RUNS IT — "Copilot · gpt-5.4" (docs/COPILOT_BACKEND.md D18): the agent
 * and the model it was set to, muted, beside the project. Only where there is
 * an agent to tell apart (`agentNamed`), so a Claude-only header is the header
 * it always was.
 */
export function TaskPeekAgent({ task }: { task: Task }) {
  useAgentBackends();
  if (!agentNamed(task.backend)) return null;
  const model = task.model ? modelLabelsOf(task.backend)[task.model] ?? task.model : "";
  const line = model ? `${agentLabel(task.backend)} · ${model}` : agentLabel(task.backend);
  return <span className="task-side-peek-project" title={line}>{line}</span>;
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
