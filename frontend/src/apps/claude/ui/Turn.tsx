// One transcript row: a right-aligned user bubble, or a full-width assistant
// reply behind the spark avatar (T:13446-13504, and the skin note at T:7-12 —
// "right-aligned user bubbles, full-width assistant prose behind a spark
// avatar", the shape claude.ai / v0 / ChatGPT all settled on).
import { memo } from "react";

import { cn } from "@platform/lib/utils";

import type { Turn as TurnRow, UserTurn } from "../protocol/controller-api";
import type { Viewable } from "./attachApi";
import { Caret } from "./Caret";
import { ClaudeMark } from "./ClaudeMark";
import { MarkdownView } from "./MarkdownView";
import { Receipts } from "./Receipts";
import { SegmentView } from "./SegmentView";
import { TroubleMessage } from "./TroubleView";

/** THE CLI'S OWN INTERRUPT MARKER (R2-2). When a turn is cut short the Claude
 *  Code CLI writes this exact string into the transcript as a USER-ROLE record —
 *  it is not something the reader typed, and drawn as a user bubble it reads as
 *  the reader having sent those five words to the model. Matched on the exact
 *  text, which is the only thing the record carries that tells it apart from a
 *  real prompt (the role, the uuid and the timestamp are a prompt's).
 *
 *  Exported so the tests, and any future history mapper that would rather stamp
 *  a note row at parse time, name the same string once. */
export const INTERRUPT_MARK = "[Request interrupted by user]";

/** Is this user row the CLI's interrupt marker rather than a prompt? Trimmed,
 *  because the record has carried a trailing newline in some CLI builds; NOT
 *  case-folded or fuzzy — a prompt that happens to talk about interrupts must
 *  still render as what the reader wrote. */
export function isInterruptMark(text: string | undefined): boolean {
  return (text ?? "").trim() === INTERRUPT_MARK;
}

export interface TurnProps {
  turn: TurnRow;
  /** The turn a `?msg=` link addressed — a halo that fades (T:1699-1713). */
  anchored?: boolean;
  /** "What was sent": the raw outgoing text, wire blocks and all (T:10970).
   *  Offered only when the turn actually carries one, and reached through the
   *  RECEIPT LINE itself wherever the turn has one (R4-1) — see below. */
  onShowSent?: (turn: UserTurn) => void;
  /** The receipt rows under a sent user turn (PR2): what this message carried,
   *  from the turn's own `attachments` or from its `raw` wire (T:10815). */
  onOpenShot?: (shot: Viewable) => void;
  /** "preview" / "app" — every noun in a receipt names the pane (T:7073). */
  paneNoun?: string;
  /** The typer's attachment INSIDE this turn, when it has one: `index` is the
   *  growing segment, or -1 for the flat body of a turn with no segments, and
   *  `text` is the frame's slice (protocol/segments.ts `streamingTailOf`).
   *  Absent for every turn the typer is not pointed at. */
  tail?: { index: number; text: string; cursor: boolean } | null;
  /** Parked cards for the live turn, at its tail (T:14728). */
  children?: React.ReactNode;
  /** segment index → what is drawn right after that segment: a resolved card
   *  sitting under the tool chip it answered (#18, Transcript's `parkPlan`). */
  cardsAfter?: Map<number, React.ReactNode> | null;
}

/** MEMOIZED. Every 400 ms poll replaces `state.turns`, but a SETTLED turn's own
 *  row object is carried over unchanged — so the whole tree under it, markdown
 *  parse included, is skipped. Its props are all stable for such a turn: `tail`
 *  reaches only the streaming one, and `children` (a parked card's stack) is
 *  passed only to the turns that actually hold one (Transcript's `parkedIn`). */
export const Turn = memo(function Turn({
  turn,
  anchored,
  onShowSent,
  onOpenShot,
  paneNoun,
  tail,
  children,
  cardsAfter,
}: TurnProps) {
  if (turn.role === "user" && isInterruptMark(turn.text)) {
    // A STATUS LINE, not a bubble (R2-2). Same shape as a `note` turn — the
    // muted centred one-liner every other "this happened beside the
    // conversation" row uses — so the log reads as the reply having been cut
    // short rather than as the reader having said something odd. Rendered here
    // rather than mapped in the protocol layer so the live poll and a replayed
    // history get the identical row with no second detector to keep in step.
    return (
      <div className="turn note is-interrupt">
        <span className="eye" aria-hidden="true">
          ⏹
        </span>
        <span>Interrupted by you</span>
      </div>
    );
  }
  if (turn.role === "user") {
    // Is there a "what was sent" to open at all? Only when the composed wire
    // differs from what the reader typed — otherwise the panel would show the
    // bubble back to them.
    const sent = onShowSent && turn.raw && turn.raw !== turn.text ? onShowSent : null;
    return (
      <div
        className={cn("turn", "user", anchored && "is-anchored")}
        // The uuid a `?msg=` link addresses. On the element itself, because the
        // anchor is looked for right after the append (T:13459-13463).
        {...(turn.uuid ? { "data-msg": turn.uuid } : {})}
      >
        <div className="bubble">{turn.text}</div>
        {/* SIBLINGS of the bubble, not wrappers around it: the re-attach probe
            matches on `.user .bubble`'s text, and folding a receipt inside
            would make every such turn stop matching (T:16584-16588). Legacy's
            order too — the attachment rows first, then the push channel's own
            line (T:16565-16596). */}
        {onOpenShot ? (
          <Receipts
            turn={turn}
            paneNoun={paneNoun ?? "preview"}
            onOpenShot={onOpenShot}
            {...(onShowSent ? { onShowSent } : {})}
          />
        ) : null}
        {turn.appState ? (
          // The push channel's receipt (T:16588-16596, `.user .attach` T:1802):
          // this message carried a description of the app the user is looking at.
          //
          // THE RECEIPT IS THE DOOR (R4-1, Akshil). It says the message carried
          // more than the bubble shows, so it is the one line a reader who wants
          // to see that "more" points at — exactly what T does with it
          // (T:11059 `row.title = "Click to see exactly what was sent to the
          // agent"`, T:1249). A second "what was sent" control beside it was
          // two doors into one room, and the wordier of them was the one that
          // only appeared on hover. So the receipt becomes a real `button` —
          // keyboard-reachable, with the pointer and the underline to say so —
          // and keeps its 11px faint typography (T:1804) unchanged.
          sent ? (
            <button
              type="button"
              className="attach is-door"
              title="Click to see exactly what was sent to the agent"
              onClick={() => sent(turn)}
            >
              app state attached
            </button>
          ) : (
            <div className="attach">app state attached</div>
          )
        ) : sent ? (
          // NO RECEIPT, but the wire still carries blocks the bubble does not
          // show (a turn from before the pane was open, a pasted attachment):
          // there is no line to make the door, so the hover affordance stays.
          <button type="button" className="sentbtn" onClick={() => sent(turn)}>
            what was sent
          </button>
        ) : null}
      </div>
    );
  }
  if (turn.role === "error") {
    // T:13698 `addError`'s plain red row: a failure that landed HERE, kept in
    // the log where it happened. Never markdown — the message is agent.py's or
    // the CLI's bytes — but not one run-on paragraph either: `TroubleMessage`
    // puts the instruction on its own line and makes the help URL a link, and
    // leaves anything it does not recognise as the single text node it was.
    return (
      <div className="turn error">
        <TroubleMessage text={turn.text} />
      </div>
    );
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
        {/* The Claude mark, not the `✻` the port shipped: a six-pointed
            asterisk at 12px reads as a snowflake, and it was a different shape
            on every platform font (#5/#15). `ui/ClaudeMark`. */}
        <span className="dot" aria-hidden="true">
          <ClaudeMark size={0.95} />
        </span>
        <span className="body">
          {/* Never both: the text segments join back to exactly the flat
              `text`, so rendering both would print the reply twice
              (T:13486-13504). */}
          {segments.length ? (
            <SegmentView segments={segments} tail={tail} cardsAfter={cardsAfter ?? null}>
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
