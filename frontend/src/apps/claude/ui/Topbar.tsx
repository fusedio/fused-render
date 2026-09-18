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
// THE QUEUE'S WORDS, from the one builder three surfaces share — "1st in line ·
// behind TASK-046" (platform/lib/queue). The header says the same sentence the
// Tasks row says, because it is the same fact about the same task.
import {
  QUEUE_PRIORITY_GLYPH,
  type QueueCaption,
  queueCaption,
  type QueueFacts,
} from "@platform/lib/queue";
// THE TASK PANEL'S OWN IDENTITY BLOCK (shell/TaskPeekWho.tsx). Imported rather
// than restated: the side peek and this header are two windows onto one
// conversation, and a reader who moves between them must not have to pair up
// two different headers (Akshil, 2026-09-14). Its CSS is `styles/task-peek.css`,
// which the shell loads for every page through `shell.css` — the chat draws
// inside that document, so there is nothing to import here.
// THE LIST'S OWN STATUS RING AND THE LIST'S OWN CAPTION. A queued task wears a
// dashed ring on the Tasks page (`schedule-ring--queued`, styles/schedule.css)
// followed by its place in the line (`QueueCaptionText`), and the chat header
// now wears both — reused rather than restated, so the two cannot drift into two
// vocabularies for one state (the drift this whole header was written against).
//
// AND IN THE SAME ORDER THE ROW READS IN: ring, number, title, caption, left to
// right (Akshil, 2026-09-18). The first cut hung the ring and the words off the
// RIGHT end of the header, in the usage limit's red seat — which made one state
// look like two different objects on two surfaces three pixels apart, and made
// waiting look like failing.
import { QueueCaptionText, StatusIcon } from "@shell/ScheduleTaskViews";
import { TaskPeekProject, TaskPeekWho } from "@shell/TaskPeekWho";
import { shortTaskId } from "@shell/tasks-lib";
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
  /**
   * THE OTHER WORD THIS SEAT CAN SAY — "paused · resumes 4:00 AM", when the
   * plan's usage limit stopped this session (`platform/lib/usage-limit`).
   *
   * It REPLACES "running" rather than sitting beside it: they are answers to one
   * question ("is anything happening here"), and a limited session is precisely
   * one where nothing is. "" on every ordinary chat.
   */
  status?: string;
  /**
   * THIS CONVERSATION'S OWN QUEUE FACTS (`useSchedule.row`) — the live
   * `/api/tasks` row, off the change feed.
   *
   * The Tasks page has always drawn a queued chat as a dashed yellow ring and a
   * caption; this pane drew nothing at all, so a reader whose send was sitting
   * behind somebody else's run had a header that looked exactly like a chat that
   * was idle (Akshil, 2026-09-17). Nothing new is invented for it: the ring is
   * the list's `StatusIcon` and the words are `queueCaption`'s.
   *
   * Only `status === "queued"` draws — `queueCaption` answers null for every
   * other row, and a running conversation already has the ring in `TaskPeekWho`
   * and the composer's own clock to say so.
   */
  queue?: (QueueFacts & Pick<Partial<Task>, "task_id" | "title">) | null;
}

export function Topbar({
  sessionId,
  subtitle,
  taskId,
  task,
  pending,
  home,
  running,
  status,
  queue,
}: TopbarProps) {
  // Null on every row that is not waiting in a line, which is the common case.
  const line = queue ? queueCaption(queue) : null;
  // ONE ROW FOR THE RING AND THE WORDS (Bugbot, PR #1194, eighth round). The
  // identity block came from the SESSION's row (`useSessionTask`) and the
  // caption from the chat's LIVE row (`useSchedule.row`, pending key first) —
  // two rows once a done chat's next send queued under `pending:<entry>`: a
  // done ring and the old title beside "2nd in line". While the line says
  // queued, the identity is the queued row's own — its status draws the dashed
  // ring, its number and title are the message that is waiting — over the
  // session row's project and target, which the queued row does not carry.
  //
  // ONLY THE THREE FACTS THE IDENTITY BLOCK DRAWS come from the queued row
  // (Bugbot, PR #1194, ninth round): its number, its title and its ring's
  // status. The live row is a whole listing row, and a send queued into
  // ANOTHER folder's task is keyed `pending:<entry>` with that folder as its
  // project — spread over the session row it painted the other folder on this
  // chat's header. Project, target and session stay the open chat's.
  const who: Task | null =
    task && line && queue
      ? ({
          ...task,
          ...(queue.task_id ? { task_id: queue.task_id } : {}),
          ...(queue.title ? { title: queue.title } : {}),
          status: "queued",
        } as Task)
      : (task ?? null);
  if (who) {
    return (
      // THE FULL SESSION ID STAYS REACHABLE. The peek's identity block prints
      // TASK-nnn and the title, which is the reader's question — but the hash is
      // what a log line, a bug report or `claude --resume` is addressed by, and
      // in this branch there is no `.c-session` span left carrying it. The
      // tooltip goes on the line itself rather than on a span of its own, so the
      // header spends no width on a string nobody reads on purpose (T:12700).
      <div
        className={"c-topbar" + (line ? " is-queued" : "")}
        title={sessionId || undefined}
      >
        {/* THE RING IS INSIDE THIS BLOCK, at the left end, and the row's own
            `status: "queued"` is what draws it dashed (taskColumn → StatusIcon).
            Nothing queued-specific is passed: the peek block already says what
            this task is doing, and this header only has to stop hiding it. */}
        <TaskPeekWho task={who} running={line ? false : running} />
        {/* …AND THE PLACE IN THE LINE TRAILS THE TITLE, exactly where a Tasks
            row puts it, rather than at the far end of the header. */}
        {line ? <QueuedCaption line={line} /> : null}
        <TaskPeekProject task={who} {...(home ? { home } : {})} />
        {/* THE PAUSED WORD STILL LANDS HERE (`status`, platform/lib/usage-limit):
            the ring says running or not, and "resumes 4:00 AM" is the one fact
            about this conversation the ring cannot carry. */}
        {status ? (
          <span className="c-tb-paused" aria-live="polite">
            {status}
          </span>
        ) : null}
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
  // …AND A QUEUED LINE OUTRANKS THE SKELETON (Akshil, 2026-09-18). A chat whose
  // first message is still keyed `pending:<entry id>` is precisely the one with
  // no task row to read, and it is also the one the reader most needs told: a
  // placeholder over a KNOWN state would hide the only answer the header has.
  if (pending && !task && !line) {
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
    shortTaskId(taskId || knownTaskId(sessionId) || "") ||
    (sessionId ? sessionId.slice(0, 8) : "");

  return (
    <div className={"c-topbar" + (line ? " is-queued" : "")}>
      {/* THE RING TAKES THE MARK'S SEAT rather than sitting beside it: the line
          keeps ONE glyph at its left end, and on a waiting chat the fact worth a
          glyph is that it is waiting — the ✻ names the tool, which the page says
          in three other places. Same order as the header above and as a Tasks
          row: ring, name, caption. */}
      {line ? (
        <span className="task-side-peek-status" title="Queued">
          <StatusIcon status="queued" />
        </span>
      ) : (
        <ClaudeMark className="c-spark" />
      )}
      <span className="c-tb-title">Claude</span>
      {line ? <QueuedCaption line={line} /> : null}
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
      {status ? (
        // NOT `.c-tb-run`: that word shimmers, and a shimmer on "paused" would
        // animate the one state whose whole content is that nothing is moving.
        <span className="c-tb-paused" aria-live="polite">
          {status}
        </span>
      ) : running ? (
        <span className="c-tb-run" aria-live="polite">
          running
        </span>
      ) : null}
    </div>
  );
}

/**
 * WHERE THIS CHAT STANDS IN ITS FOLDER'S LINE — "1st in line · behind TASK-046".
 *
 * THE TASKS ROW'S OWN MARKUP, classes and all (`tasks-row-queue` /
 * `tasks-queue-text`, styles/tasks.css, which `shell.css` loads for every page —
 * the chat draws inside that document). The words are `queueCaption`'s, the
 * pointer's title and the ⤒ are the row's, and the ink is the row's muted
 * register: one fact, one sentence, one look, wherever a reader meets it.
 *
 * IT IS NOT THE USAGE LIMIT'S SEAT any more (`.c-tb-paused`, which keeps its one
 * job): that span is Blocked's red at the right end of the line, and a queued
 * chat is neither blocked nor at the end of anything — it is a task with a place
 * in a line, and the place belongs beside the name it is a place for.
 *
 * `aria-live="polite"`: an ambient state, mentioned when the reader next comes up
 * for air. The ring beside it already says "Queued" out loud.
 */
function QueuedCaption({ line }: { line: QueueCaption }) {
  return (
    <span
      className={"tasks-row-queue" + (line.runsNext ? " is-next" : "")}
      data-hint={line.aheadTitle || line.text}
      aria-live="polite"
    >
      {line.runsNext && (
        <span className="tasks-queue-glyph" aria-hidden="true">
          {QUEUE_PRIORITY_GLYPH}
        </span>
      )}
      <span className="tasks-queue-text">
        <QueueCaptionText queue={line} />
      </span>
    </span>
  );
}
