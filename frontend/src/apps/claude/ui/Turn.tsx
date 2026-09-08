// One transcript row: a right-aligned user bubble, or a full-width assistant
// reply behind the spark avatar (T:13446-13504, and the skin note at T:7-12 —
// "right-aligned user bubbles, full-width assistant prose behind a spark
// avatar", the shape claude.ai / v0 / ChatGPT all settled on).
import { memo } from "react";

import { cn } from "@platform/lib/utils";

import type { Turn as TurnRow, UserTurn } from "../protocol/controller-api";
import { Caret } from "./Caret";
import { MarkdownView } from "./MarkdownView";
import { SegmentView } from "./SegmentView";

export interface TurnProps {
  turn: TurnRow;
  /** The turn a `?msg=` link addressed — a halo that fades (T:1699-1713). */
  anchored?: boolean;
  /** "What was sent": the raw outgoing text, wire blocks and all (T:10970).
   *  Offered only when the turn actually carries one. */
  onShowSent?: (turn: UserTurn) => void;
  /** The typer's attachment INSIDE this turn, when it has one: `index` is the
   *  growing segment, or -1 for the flat body of a turn with no segments, and
   *  `text` is the frame's slice (protocol/segments.ts `streamingTailOf`).
   *  Absent for every turn the typer is not pointed at. */
  tail?: { index: number; text: string; cursor: boolean } | null;
  /** Parked cards for the live turn, at its tail (T:14728). */
  children?: React.ReactNode;
}

/** MEMOIZED. Every 400 ms poll replaces `state.turns`, but a SETTLED turn's own
 *  row object is carried over unchanged — so the whole tree under it, markdown
 *  parse included, is skipped. Its props are all stable for such a turn: `tail`
 *  reaches only the streaming one, and `children` (a parked card's stack) is
 *  passed only to the turns that actually hold one (Transcript's `parkedIn`). */
export const Turn = memo(function Turn({ turn, anchored, onShowSent, tail, children }: TurnProps) {
  if (turn.role === "user") {
    return (
      <div
        className={cn("turn", "user", anchored && "is-anchored")}
        // The uuid a `?msg=` link addresses. On the element itself, because the
        // anchor is looked for right after the append (T:13459-13463).
        {...(turn.uuid ? { "data-msg": turn.uuid } : {})}
      >
        <div className="bubble">{turn.text}</div>
        {onShowSent && turn.raw && turn.raw !== turn.text ? (
          <button type="button" className="sentbtn" onClick={() => onShowSent(turn)}>
            what was sent
          </button>
        ) : null}
      </div>
    );
  }
  if (turn.role === "error") {
    // T:13698 `addError`'s plain red row: a failure that landed HERE, kept in
    // the log where it happened. A text node, never markdown — the message is
    // agent.py's or the CLI's bytes.
    return <div className="turn error">{turn.text}</div>;
  }
  if (turn.role === "note") {
    // A one-liner for something that happened BESIDE the conversation: the
    // agent reading the app state on its own, a skill it reached for, the user
    // ending a turn (T:13718-13734).
    return (
      <div className="turn note">
        <span className="eye" aria-hidden="true">
          {turn.glyph}
        </span>
        <span>{turn.text}</span>
      </div>
    );
  }
  const segments = turn.segments ?? [];
  return (
    <>
      <div className={cn("turn", "assistant", anchored && "is-anchored")}>
        <span className="dot" aria-hidden="true">
          ✻
        </span>
        <span className="body">
          {/* Never both: the text segments join back to exactly the flat
              `text`, so rendering both would print the reply twice
              (T:13486-13504). */}
          {segments.length ? (
            <SegmentView segments={segments} tail={tail}>
              {children}
            </SegmentView>
          ) : (
            <>
              {/* The legacy flat bubble (`index: -1`): the typer streams the
                  body itself, with the caret after it (T:15063-15066). */}
              <MarkdownView
                className="seg-text"
                text={tail ? tail.text : turn.text}
                enhance={!tail}
              />
              {tail && tail.cursor ? <Caret /> : null}
              {children}
            </>
          )}
        </span>
      </div>
      {/* The run was stopped by the user: a note beside the conversation, not
          part of the reply (T:13718-13734, runEnding's "Stopped."). */}
      {turn.stopped ? (
        <div className="turn note">
          <span className="eye" aria-hidden="true">
            ⏹
          </span>
          <span>Stopped.</span>
        </div>
      ) : null}
    </>
  );
});

export default Turn;
