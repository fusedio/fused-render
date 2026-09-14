// The chat's IDENTITY LINE — T's `#topbar` (T:1285-1310, 4030-4083, inventory
// 04 §F): the Claude mark, "Claude", the target's name, then the task number and
// the "running" mark at the right end.
//
// IT USED TO BE TWO ROWS, and the first of them was a second control strip: back
// at its left end, the ⋮ at its right, and the seat PR2's screenshot button was
// meant to land in. T has no such row — its `#anntools` is ONE strip above both
// views carrying `← Chats`, the three preview seats and the ⋮ together, and
// `#chat.home #topbar` (T:1277) hides only THIS line on the landing. Rendering
// our own copy of the controls here put the seats on a row of their own above
// the row that held the menu (Akshil, 2026-09-09, P2-1), so back and the kebab
// moved up into the shared strip (`ClaudeChat`'s `.c-anntools`) and what is left
// is the identity, which is all T ever had here.
//
// The name is TASK-nnn and not a session hash: a session IS a task, 1:1, and
// TASK-023 is the identifier every other surface in this app prints, so a
// reader can carry it to the Tasks page and quote it to someone (T:12660-12672).
//
// AND ONCE THE LISTING HAS A ROW FOR THIS SESSION, the whole line is the TASK
// SIDE PEEK'S identity block instead (Akshil, 2026-09-14): the status ring, the
// number, the task's title and the project it runs in. The ✻ Claude wordmark and
// the target's path are facts about the tool and the file — true, and printed
// twice over elsewhere on the page — where the reader's question in a chat is
// "which conversation am I in, and how is it doing". The peek answers exactly
// that, so the chat borrows the peek's own component rather than growing a
// second header that must be kept looking like it.
import type { Task } from "@platform/lib/api";
// THE TASK PANEL'S OWN IDENTITY BLOCK (shell/TaskPeekWho.tsx). Imported rather
// than restated: the side peek and this header are two windows onto one
// conversation, and a reader who moves between them must not have to pair up
// two different headers (Akshil, 2026-09-14). Its CSS is `styles/task-peek.css`,
// which the shell loads for every page through `shell.css` — the chat draws
// inside that document, so there is nothing to import here.
import { TaskPeekProject, TaskPeekWho } from "@shell/TaskPeekWho";
import "../styles/composer.css";
// The skeleton below wears the landing list's own placeholder bar
// (`.c-skel-bar`, `home.css`), which is the one shimmer this app draws. Imported
// here so the rule travels with the component that spends it rather than by
// luck of what else the bundle happened to pull in.
import "../styles/home.css";
import { ClaudeMark } from "./ClaudeMark";
import { knownTaskId } from "./Kebab";

export interface TopbarProps {
  sessionId: string;
  /** The target's own name under "Claude" (`#banner-sub`). */
  subtitle?: string;
  /** TASK-nnn, when the listing has been read (`#session`). */
  taskId?: string;
  /**
   * THE LISTING'S ROW FOR THIS SESSION, when there is one (`useSessionTask`).
   * With it the header IS the task — status ring, number, title, project — and
   * without it the line below is drawn exactly as it always was.
   *
   * "Without it" is a real state and not an error: a chat seconds old has a
   * session id before `/api/tasks` has a row for it, and a header that appeared
   * a moment after the transcript did would be worse than one that fills in.
   */
  task?: Task | null;
  /**
   * NOBODY HAS ANSWERED FOR THIS SESSION YET (`useSessionTask`'s `pending`).
   *
   * The line below is a CLAIM — "this conversation has no task row, so here is
   * the tool's own name and the file" — and it is true of exactly one thing: a
   * chat so new the server's watcher has not seen its transcript. A deep link
   * into an old conversation is not that, and printing the claim while
   * `/api/tasks` reads 800 rows meant every such arrival wore the wrong
   * identity and then swapped (Akshil, 2026-09-14). While the answer is
   * genuinely unknown the header says so, in the same placeholder bar the
   * landing's Recent list draws.
   */
  pending?: boolean;
  /** For the project chip's `~` in its tooltip. Absent in the chat, which knows
   *  no home directory of its own — the tooltip then spells the path in full,
   *  which is the same path. */
  home?: string;
  /** A turn is live: one source of truth for the mark and the composer's stop
   *  square (T:1342-1349). */
  running: boolean;
}

export function Topbar({
  sessionId,
  subtitle,
  taskId,
  task,
  pending,
  home,
  running,
}: TopbarProps) {
  if (task) {
    return (
      // THE FULL SESSION ID STAYS REACHABLE. The peek's identity block prints
      // TASK-nnn and the title, which is the reader's question — but the hash is
      // what a log line, a bug report or `claude --resume` is addressed by, and
      // in this branch there is no `.c-session` span left carrying it. The
      // tooltip goes on the line itself rather than on a span of its own, so the
      // header spends no width on a string nobody reads on purpose (T:12700).
      <div className="c-topbar" title={sessionId || undefined}>
        <TaskPeekWho task={task} />
        <TaskPeekProject task={task} {...(home ? { home } : {})} />
        {/* NO "running" WORD HERE (Akshil, 2026-09-14): the ring at the left
            already says it, and one line saying one thing twice spends the
            header's last inch on nothing. The page's own turn clock still
            drives the composer's stop square; the header defers to the ring. */}
      </div>
    );
  }

  // …AND THE UNKNOWN, WHICH IS NEITHER (Akshil, 2026-09-14). One ring-sized dot
  // and one bar — the shape of the identity block that is about to land, at the
  // place it will land — so the header's height and rhythm do not move when it
  // does. `aria-hidden`, because there is nothing here to read out: the line has
  // no name yet, and announcing a placeholder is worse than announcing nothing.
  //
  // A TASK ALWAYS OUTRANKS IT, stated rather than left to the branch order
  // above (Akshil QA, 2026-09-14): `pending` is "nobody has answered", and a
  // header holding a row HAS its answer — a skeleton over it would be the
  // placeholder hiding the very thing it stands in for.
  if (pending && !task) {
    return (
      <div className="c-topbar" title={sessionId || undefined}>
        <span className="c-tb-skel" aria-hidden="true">
          <span className="c-skel-dot" />
          <span className="c-skel-bar is-title" />
        </span>
      </div>
    );
  }

  // The number, or the session hash until it lands (T:12698 — the answer to a
  // slow listing is the old label, never a gap). Computed HERE and not above the
  // branch: only this line prints it.
  const label =
    taskId ||
    knownTaskId(sessionId) ||
    (sessionId ? sessionId.slice(0, 8) : "");

  return (
    <div className="c-topbar">
      <ClaudeMark className="c-spark" />
      <span className="c-tb-title">Claude</span>
      <span className="c-tb-file" title={subtitle || undefined}>
        {subtitle ?? ""}
      </span>
      {label ? (
        // The full session id stays reachable for the reader who actually wants
        // it (a log line, a bug report) without spending header width on it
        // (T:12700).
        <span className="c-session" title={sessionId || undefined}>
          {label}
        </span>
      ) : null}
      {/* `aria-live="polite"`, not assertive: this is an ambient state, and a
          reader that interrupts to announce the start of every turn is worse
          than one that mentions it when it next comes up for air (T:4078). */}
      {running ? (
        <span className="c-tb-run" aria-live="polite">
          running
        </span>
      ) : null}
    </div>
  );
}
