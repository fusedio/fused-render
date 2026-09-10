// THE BANNER A BLOCKED COMPOSER WEARS (T:4122-4147 markup, T:17088-17165
// `renderSchedBlock`, CSS T:1472-1671; inventory 05 §C).
//
// A BANNER, NOT A MODAL: the transcript stays readable and the session list
// stays reachable. It carries ONE action, and that action is whichever one
// actually reopens the box — cancel for a one-off, stop-the-repeat for a repeat
// (cancelling an occurrence of a repeat unblocks nothing, because `_materialize`
// arms the next one and it blocks again). "Edit schedule" used to sit beside it
// and is GONE: the row now lands on the task where Edit lives with its whole
// popover, so the banner was two doors to one room and its own door did not
// unblock (Akshil, 2026-08-17).
//
// WHAT IT SHOWS is the Tasks LIST view's own row — a status ring, TASK-nnn, the
// name, and the state and time at the right end — because a reader who knows
// the Tasks page already knows how to read this row, and reusing that vocabulary
// is what makes the banner a view of the schedule rather than a paragraph about
// it. NOT on the row: the folder and the session id. "You will not show the
// folder because you are already in that folder, you are not showing the session
// ID because you are already there" (Akshil, 2026-08-17).
import { useCallback, useEffect, useRef } from "react";
import "../styles/sched.css";
import {
  schedIsRepeat,
  schedRefusalNote,
  schedRowName,
  schedRowState,
  schedRowTitle,
  schedStopLabel,
  schedStopTitle,
  schedWhenText,
  schedWhyLine,
  type SchedEntry,
  type SchedTask,
} from "../sched/scheduled";

export interface SchedBlockProps {
  /** Pending messages aimed at this conversation, soonest first. Empty renders
   *  nothing at all — no reserved space, no empty strip (T:17091). */
  blockers: readonly SchedEntry[];
  /** The `/api/tasks` row for `blockers[0]`, or null: an unreadable listing
   *  costs the number and the state, never the row and never the block. */
  rec: SchedTask | null;
  /** The entry whose stop has been pressed once (repeats only). */
  armed: boolean;
  /** The entry whose cancel came back refused. */
  refused: boolean;
  /** A cancel is in flight — the button is dead for its duration. */
  stopping: boolean;
  /** `Date.now()` as of the last schedule poll (`useSchedule.tick`). The time
   *  cell is a function of the wall clock, not of the entry — "14:00 today"
   *  becomes "any moment now" with every field of the entry unchanged — so the
   *  poll hands the clock in rather than leaving the cell to whichever render
   *  the entry happens to trigger. */
  tick?: number;
  onStop(): void;
  /** The row's hop: the Tasks page, on the calendar. */
  onRow(): void;
  /** The card's own node, for the outside-press disarm (`useSchedule`). */
  cardRef?: React.MutableRefObject<HTMLDivElement | null>;
}

export function SchedBlock({
  blockers,
  rec,
  armed,
  refused,
  stopping,
  tick,
  onStop,
  onRow,
  cardRef,
}: SchedBlockProps) {
  const nameRef = useRef<HTMLSpanElement | null>(null);
  const next = blockers[0];

  /**
   * THE STRAY SECOND COPY, and its fix (T:17168-17187). The old card wrote the
   * message onto a `title` unconditionally, so a message short enough to fit
   * rendered once in the row and again in the native tooltip the moment the
   * pointer rested on it. Now the full name is offered only when the cell
   * actually clipped it — and MEASURED WHEN THE POINTER ARRIVES, not when the
   * row is drawn: at render time the pane may still be laying out (the first
   * paint reports every cell as clipped) and a title written then would outlive
   * the layout that justified it.
   */
  const nameTitle = useCallback(() => {
    const el = nameRef.current;
    if (!el) return;
    if (el.scrollWidth > el.clientWidth + 1) el.title = el.textContent || "";
    else el.removeAttribute("title");
  }, []);

  const repeat = next ? schedIsRepeat(next) : false;
  /** THE ENTRY, NOT THE TASK. `rec` is the row for the whole task and answers
   *  `done` for a task holding a finished run and a future pending message —
   *  which is a true sentence about the board and a false one about the message
   *  holding this box shut (FIX-B). */
  const { state, label } = schedRowState(rec, next);
  /** ONE POLL, ONE READ. `tick` moving is both the re-render and the clock the
   *  cell is read against; `Date.now()` is the fallback for a caller that hands
   *  no poll in (and for the render that precedes the first one). */
  const when = next ? schedWhenText(next.due, new Date(tick || Date.now())) : "";
  /** The MESSAGE that is coming, and only then the conversation's title — the
   *  banner's question is "what is about to run?" and the blocker's own words
   *  answer it (FIX-C). */
  const name = schedRowName(next, rec);

  /**
   * A TITLE BELONGS TO THE TEXT THAT WAS MEASURED (T:17161-17163). T drops it on
   * every render of the card, because the render just replaced the very string
   * the measurement was taken of — `nameTitle` writes the next one when there is
   * a pointer to write it for. Here the node survives the re-render, so the
   * stale tooltip has to be removed explicitly: without this, the full text of
   * the pre-`rec` message goes on hovering a row that now reads something else,
   * until the pointer leaves and comes back.
   */
  useEffect(() => {
    nameRef.current?.removeAttribute("title");
  }, [name]);

  if (!next) return null;

  return (
    <div className="c-schedblock">
      {/* `role=status`: the box going dead is news, and the reason has to reach
          a reader who is not looking at this corner of the pane. */}
      <div className="sb-card" role="status" ref={cardRef}>
        <div className="sb-why">
          <span className="sb-ic" aria-hidden="true">
            ⏱
          </span>
          <span className="sb-when">{schedWhyLine(blockers)}</span>
        </div>
        {/* A BUTTON because the hop is a pushState into the React shell rather
            than a document load — an <a href> here would be a link that cannot
            be trusted to a middle click. First in the DOM, so it is first in tab
            order too, ahead of the destructive control. */}
        <button
          type="button"
          className="sb-row"
          title={schedRowTitle(rec)}
          onClick={onRow}
          onPointerEnter={nameTitle}
          onFocus={nameTitle}
        >
          <span className={"sb-ring sb-ring--" + state} aria-hidden="true" />
          {/* An EMPTY id is "/api/tasks could not be read", not "this task has
              no number": the cell disappears (`:empty`) rather than holding a
              gap open. */}
          <span className="sb-id">{(rec && rec.task_id) || ""}</span>
          <span className="sb-name" ref={nameRef}>
            {name}
          </span>
          {/* THE spacer, and there is exactly one: a second `margin-left:auto`
              in the same row centres the right-hand group instead of pinning
              it (tasks.css's own house rule). */}
          <span className="sb-grow" />
          <span className="sb-meta">{label + " · " + when}</span>
        </button>
        <div className="sb-note">{refused ? schedRefusalNote(repeat) : ""}</div>
        <div className="sb-acts">
          <button
            type="button"
            className={armed ? "armed" : undefined}
            title={schedStopTitle(repeat)}
            disabled={stopping}
            onClick={onStop}
          >
            {schedStopLabel(repeat, armed)}
          </button>
        </div>
      </div>
    </div>
  );
}
