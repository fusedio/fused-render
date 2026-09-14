// One transcript row: a right-aligned user bubble, or a full-width assistant
// reply behind the spark avatar (T:13446-13504, and the skin note at T:7-12 —
// "right-aligned user bubbles, full-width assistant prose behind a spark
// avatar", the shape claude.ai / v0 / ChatGPT all settled on).
import { memo, useId } from "react";

import { cn } from "@platform/lib/utils";

import type { AssistantTurn, Turn as TurnRow, UserTurn } from "../protocol/controller-api";
import { viewKind } from "../protocol/segments";
import { toolChipSummary } from "../protocol/summaries";
import { INTERRUPT_MARK, isInterruptMark, isMarkerOnly } from "../protocol/wire";
import type { Viewable } from "./attachApi";
import { MarkerText } from "./AttachIcon";
import { Caret } from "./Caret";
import { ClaudeMark } from "./ClaudeMark";
import { MarkdownView } from "./MarkdownView";
import { Receipts } from "./Receipts";
import { SegmentView } from "./SegmentView";
import { formatStamp, stampTitle } from "./stamp";
import { TroubleMessage } from "./TroubleView";

/** THE INTERRUPT MARKER, RE-EXPORTED. It now lives in `protocol/wire.ts`
 *  because `protocol/recap.ts` needs the same answer this file does — a record
 *  drawn as a note carries no `data-msg`, so it is not a position anything may
 *  scroll to — and protocol may not reach into a React module for it. Kept
 *  named here so the callers that already say `from "./Turn"` still read the
 *  one definition. */
export { INTERRUPT_MARK, isInterruptMark };

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
  /** IS THIS ASSISTANT TURN FOLDED to its first line (design.md §B)? The rule
   *  that decides it lives in `Transcript` — every settled reply but the last
   *  one — because it is a statement about the log, not about a row. A turn
   *  that cannot be folded (below) ignores it. */
  collapsed?: boolean;
  /** The reader's click on the ✻ mark, with this turn's key. ONE callback for
   *  the whole log rather than one closure per turn: a fresh function per row
   *  per render is a changed prop, and `Turn`'s memo is what keeps a settled
   *  turn's markdown from being re-parsed on every 400 ms poll. */
  onToggleCollapse?: (key: string) => void;
  /** This turn is holding a card the reader has not answered yet. Such a turn
   *  is never foldable — the run is blocked on something inside it, and folding
   *  the block away is folding away the thing to do (design.md §B). */
  pendingCard?: boolean;
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
  collapsed = false,
  onToggleCollapse,
  pendingCard,
}: TurnProps) {
  // BEFORE the early returns below: a hook may not sit behind one, and the id
  // is only used on the assistant branch (see `bodyId`).
  const uid = useId();
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
    // WHEN it was sent (design.md §C): epoch seconds, off the transcript
    // record for a restored turn (agent.py `_row_ts`) and off the controller
    // for a live send. Absent on an old transcript, and then no time is drawn.
    const ts = turn.ts;
    const stamp = formatStamp(ts);
    return (
      <div
        className={cn("turn", "user", anchored && "is-anchored")}
        // The uuid a `?msg=` link addresses. On the element itself, because the
        // anchor is looked for right after the append (T:13459-13463).
        {...(turn.uuid ? { "data-msg": turn.uuid } : {})}
      >
        {/* THE LEFT ICON LANE — the column an assistant turn's ✻ mark sits in,
            which on a right-aligned user row is empty space. On hover only: a
            time under every message is a second column of numbers down a
            conversation nobody reads, and the one message a reader wants the
            time of is the one they are pointing at (design.md §C). */}
        {stamp ? (
          <span className="turn-stamp" {...(stampTitle(ts) ? { title: stampTitle(ts)! } : {})}>
            {stamp}
          </span>
        ) : null}
        {/* A WORDLESS SEND'S BUBBLE says what the message carried instead —
            "pane screenshot", "files", "annotations" — and each of those gets
            the same lucide glyph the chip and the receipt wear (P2-7). Anything
            the reader actually typed is their own words and gets none. The
            bubble's TEXT is identical either way. */}
        <div className="bubble">
          {isMarkerOnly(turn.text) ? <MarkerText text={turn.text} /> : turn.text}
        </div>
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
        ) : null}
        {/* AND THERE IS NO "what was sent" HOVER CONTROL, ANYWHERE (P3R1-7,
            owner 2026-09-10). One survived here, for the turn whose wire differs
            with no receipt line to press — defensible in itself and confusing in
            practice: a word-shaped affordance that materialises under the
            pointer, sits below a receipt that is already the door wherever there
            is one, and reads as a second, wordier entrance to the same room.
            T ships no such control at all; the receipt row and the overview
            thumb carry the title and open the panel (T:11059, T:11072) and that
            is the whole vocabulary. A turn with a differing wire and nothing
            drawn under it now has no door, which is the honest answer: there is
            no line there to make one out of. */}
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
  // CAN THIS REPLY BE FOLDED? Not while it is streaming — the fold would be a
  // lid over something still arriving, and the toggle would be a control whose
  // meaning changes under the pointer — and not while it holds a card the
  // reader has not answered: the run is blocked on that card and it is the one
  // thing on screen to do (design.md §B).
  const foldable = !!onToggleCollapse && !turn.streaming && !pendingCard;
  // The body the mark opens and shuts, for `aria-controls`. `useId` and not the
  // turn's key: six compact chat mounts share one document on the cards wall and
  // the same conversation is replayed in several of them, so a key-derived id
  // would name three elements at once.
  const bodyId = uid + "body";
  const folded = foldable && collapsed;
  const line = folded ? collapsedLine(turn) : null;
  return (
    <>
      <div className={cn("turn", "assistant", anchored && "is-anchored", folded && "is-folded")}>
        {/* THE MARK IS THE TOGGLE (design.md §B). The one glyph every assistant
            turn already wears, in the lane the eye runs down when it is looking
            for where a reply begins — so the control for "show me less of this
            one" is exactly where the reader is already pointing, and the
            transcript gains no chrome for it. Disabled (and cursor-default)
            rather than absent while the turn cannot be folded: a control that
            vanishes mid-run is a control nobody learns.

            Not the `✻` the port shipped: a six-pointed asterisk at 12px reads
            as a snowflake, and it was a different shape on every platform font
            (#5/#15). `ui/ClaudeMark`.

            EXPANDED OF WHAT? Only a mark that actually folds something says so
            (review #7): a streaming turn's mark is `disabled`, and
            `aria-expanded` on a dead control announces a disclosure the reader
            cannot work — so the state and the body it names both arrive with
            the ability to press it, and `aria-controls` points at the body that
            opens and shuts. */}
        <button
          type="button"
          className="dot"
          aria-label={folded ? "Expand response" : "Collapse response"}
          {...(foldable
            ? { "aria-expanded": !folded, "aria-controls": bodyId }
            : {})}
          disabled={!foldable}
          onClick={foldable ? () => onToggleCollapse!(turn.key) : undefined}
        >
          <ClaudeMark size={0.95} />
        </button>
        <span className="body" id={bodyId}>
          {/* Never both: the text segments join back to exactly the flat
              `text`, so rendering both would print the reply twice
              (T:13486-13504).

              `live` is the turn's own `streaming`: it holds the trailing run
              OPEN — members individual, no trigger — while more segments can
              still land on the end of this turn (protocol/segments.ts
              `groupCollapsibles`), so the fold does not go on and come off once
              per call mid-run. It folds when the turn ends. */}
          {/* FOLDED: one ellipsized line and nothing else — no chips, no
              triggers, no parked cards. The line is the reply's own first
              sentence wherever it has one, because that is what the reader is
              scanning for; a turn that was all tool calls says what the first
              call was instead, muted, so the row is never blank. */}
          {folded ? (
            <span className={cn("turn-collapsed", line && line.muted && "is-muted")}>
              {line ? line.text : ""}
            </span>
          ) : segments.length ? (
            <SegmentView
              segments={segments}
              tail={tail}
              cardsAfter={cardsAfter ?? null}
              live={!!turn.streaming}
            >
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

/** THE ONE LINE A FOLDED REPLY SHOWS (design.md §B). */
export interface CollapsedLine {
  text: string;
  /** The reply had no prose at all and this is a tool summary standing in for
   *  it — drawn dimmer, because it is machinery rather than the answer. */
  muted: boolean;
}

/**
 * The first line of the turn's first `text` segment, or — for a turn that was
 * nothing but tool calls — the first call's own chip summary, muted.
 *
 * FIRST LINE, not first sentence: markdown's own line breaks are where the
 * reply's structure is, and a heading or a list item is exactly the fragment a
 * reader scanning a folded log wants. The ellipsis is CSS (`text-overflow`), so
 * the cut lands at the column's real width rather than at a character count.
 */
export function collapsedLine(turn: TurnRow): CollapsedLine | null {
  const segs = (turn as AssistantTurn).segments ?? [];
  for (const seg of segs) {
    if (!seg || viewKind(seg) !== "text") continue;
    const line = firstLine((seg as { text?: string }).text ?? "");
    if (line) return { text: line, muted: false };
  }
  // No segments at all is the legacy flat bubble, whose whole body is `text`.
  const flat = firstLine(turn.text ?? "");
  if (flat) return { text: flat, muted: false };
  for (const seg of segs) {
    if (seg && seg.kind === "tool") return { text: toolChipSummary(seg), muted: true };
  }
  return null;
}

/** The first line with anything on it, trimmed. */
function firstLine(text: string): string {
  for (const raw of String(text ?? "").split("\n")) {
    const line = raw.trim();
    if (line) return line;
  }
  return "";
}

export default Turn;
