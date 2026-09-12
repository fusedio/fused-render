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
import "../styles/composer.css";
import { ClaudeMark } from "./ClaudeMark";
import { knownTaskId } from "./Kebab";

export interface TopbarProps {
  sessionId: string;
  /** The target's own name under "Claude" (`#banner-sub`). */
  subtitle?: string;
  /** TASK-nnn, when the listing has been read (`#session`). */
  taskId?: string;
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
}

export function Topbar({ sessionId, subtitle, taskId, running, status }: TopbarProps) {
  // The number, or the session hash until it lands (T:12698 — the answer to a
  // slow listing is the old label, never a gap).
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
