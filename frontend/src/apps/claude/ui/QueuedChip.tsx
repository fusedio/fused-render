// THE CHIP UNDER A MESSAGE THE FOLDER WAS TOO BUSY TO TAKE (the project queue,
// prefs `queue.enabled`).
//
// What happened, from the reader's side: they pressed Enter, the bubble went up
// exactly as it always does, and nothing started — because another task is
// holding this folder. The server did not refuse the message, it put it in the
// line. So the bubble is a normal bubble and this says the one thing it cannot:
// where in the line it landed, and who is in front.
//
// IT IS THE SCHEDULED-MESSAGE BLOCK'S OWN ROW, not a second thing that looks
// like one. `.c-schedblock` and its `sb-*` skin already say "a message of yours
// is waiting" in this pane, in the composer's own column and measure, with the
// Tasks List row's vocabulary inside it (SchedBlock's header has the argument).
// A queued message IS a pending scheduled message — the server created exactly
// that — so inventing a second card for it would be two shapes for one fact,
// sitting a few pixels apart.
//
// What is different is the SHAPE OF THE CARD, and only because the question is
// smaller. SchedBlock's card is a column — a reason line, a task row, a control
// — because it has to explain why the composer is shut. Nothing is shut here
// (the composer stays open and more messages queue behind), so this is one row:
// a ring, the words, Skip and Cancel.
//
// AND IT IS THE ONLY THING DRAWING THAT ENTRY. Both used to render for one
// message — this chip saying "Queued · #1 in line · behind TASK-006" and the
// block, directly under it, saying "Blocked — a scheduled message runs in this
// chat … Cancel this message" about the very same entry. Two cards, two
// vocabularies, and a contradiction about whether anything was blocked at all
// (Akshil, browser QA 2026-09-12). Under the flag the block now filters out
// every entry a chip has (`useSchedule.chipEntryIds`) and hides itself when
// that empties it, and the one capability it had that this lacked — Cancel —
// moved here. Flag off, the block is untouched: no chips exist.
//
// TWO KINDS OF WAITING, ONE CHIP. A message the FOLDER was too busy to take is
// a scheduler entry with a place in a line (`send`); a follow-up typed into a
// turn that is already running is held by the live host instead, with no entry,
// no place and nothing to skip (`kind: "inbox"`). Same word, same ring, same
// row — because from the reader's side they are the same sentence: "this is
// waiting, in order, and it is not lost".
import "../styles/sched.css";
import { QUEUE_PRIORITY_GLYPH, queueLine } from "@platform/lib/queue";
import type { QueueFacts } from "@platform/lib/queue";

/** One queued send this chat is still holding a chip for. The entry id is its
 *  identity AND its liveness — the chip comes down when that entry stops being
 *  pending (`sched/queued-sends` reads `useSchedule.pendingIds`). */
export interface QueuedSend extends QueueFacts {
  /**
   * The scheduler entry the server created for this message — this send's
   * identity, its liveness AND the name Skip spends.
   *
   * THE ONLY NAME THAT CANNOT GO STALE. The admission also answers a task
   * `key`, and this used to hold it: `pending:<leader entry id>` for a chat
   * with no session. The moment the leader's run opens one the store REKEYS
   * that task onto the session id, and a Skip posted with the frozen key 404s —
   * silently, on a follower chip, which is exactly the chip most likely to be
   * pressed (round-2 review). An entry id is minted once and never rekeyed, and
   * `POST /api/tasks/queue/skip` takes `{entry_id}` for that reason.
   */
  entryId: string;
  /**
   * THE WORDS THIS CHIP IS ABOUT, kept here because the transcript loses them.
   *
   * The bubble a queued send posts is optimistic — a row in the live document
   * and in no file — and the thing that reliably replaces the live document is
   * the adoption this very feature causes: a chat whose leader ran adopts the
   * session it opened (`ClaudeChat`'s `adoptSession` → `openSession`), the
   * transcript is re-read from the JSONL, and every follower whose entry has NOT
   * fired is nowhere in it. So the chips stayed — their entries really are still
   * pending — over a conversation that no longer showed what they were for.
   *
   * The send carries its own words and this component draws them: ONE source of
   * truth (re-posting optimistic bubbles after the adoption would leave rows the
   * next refresh drops all over again), and the row goes with the chip when the
   * entry fires — by which time the JSONL has the real one.
   *
   * EMPTY IS A REAL VALUE: a wordless send (pictures or notes alone) has no
   * typed line, and its bubble in the transcript is markers only the controller
   * can compose. No row is drawn for one.
   */
  text: string;
}

export interface QueuedChipProps {
  /** The ordinary chip — a message the FOLDER queued. Present so the two kinds
   *  discriminate on one field; omitted everywhere it is the default. */
  kind?: "send";
  send: QueuedSend;
  /** Skip is in flight, or already spent: the button is dead either way, and it
   *  is dead rather than gone for the reason the Tasks row's is (see there). */
  busy?: boolean;
  /** Send this to the front of its folder's line. Never offered at the head. */
  onSkip(): void;
  /**
   * TAKE THE MESSAGE BACK — the one thing the card this chip replaced could do
   * and the chip could not.
   *
   * Under the queue a chip and the scheduled-message block used to draw the SAME
   * entry at the same time, a few pixels apart, in two vocabularies: "Queued ·
   * #1 in line" over "Blocked — a scheduled message runs in this chat … Cancel
   * this message". One fact, two cards, and they contradicted each other about
   * whether anything was blocked (Akshil, browser QA 2026-09-12). So the block
   * no longer draws an entry a chip is drawing, and its capability comes here.
   *
   * QUIETER THAN SKIP, and second: Skip is what a reader wants nine times out of
   * ten, and the destructive one is the one that must never be the easy press.
   * It is the same `POST /api/schedule/cancel` the block spent — one press, no
   * arming, because a queued send is a one-off and cancelling it loses exactly
   * the words still sitting on the chip.
   */
  onCancel?(): void;
  /** A cancel is in flight: both buttons are dead for its duration, because the
   *  chip is about to leave and a Skip pressed into that window would be a
   *  request about an entry that is going away. */
  cancelling?: boolean;
}

/**
 * THE FOLLOW-UPS THE LIVE RUN IS HOLDING, which is the queue's other waiting.
 *
 * A line typed into a turn that is already going does not reach the model: the
 * host takes the bytes and the CLI holds them until the turn ends. The bubbles
 * went up and nothing at all said why they were sitting there — three plain
 * bubbles and a composer footnote counting them (Akshil, browser QA
 * 2026-09-12, screenshot 4). That is the same fact as a queued send, one layer
 * in: waiting, in order, not lost. So it wears the same chip and the same word.
 *
 * NO SKIP AND NO CANCEL. They are the live host's now, not the scheduler's —
 * there is no entry to move and nothing this page can take back (a stop is what
 * strands them, and the composer's stop button is already the control for that).
 */
export interface QueuedInboxProps {
  kind: "inbox";
  /** How many follow-ups the run has not answered yet (`ChatState.queued`). */
  count: number;
}

/** "3 follow-ups · in this turn" — the inbox chip's half of the grammar, kept
 *  beside the send chip's so the two read as one sentence with one subject. */
export function inboxLine(count: number): string {
  const many = count === 1 ? "1 follow-up" : `${count} follow-ups`;
  return `${many} · in this turn`;
}

export function QueuedChip(props: QueuedChipProps | QueuedInboxProps) {
  if (props.kind === "inbox") {
    if (props.count <= 0) return null;
    return (
      <div className="c-schedblock c-queuechip">
        <div className="sb-card sb-card--chip" role="status">
          <span className="sb-ring sb-ring--queued" aria-hidden="true" />
          <span className="sb-queued-word">Queued</span>
          <ChipDot />
          <span className="sb-queued-line">{inboxLine(props.count)}</span>
          <span className="sb-grow" />
        </div>
      </div>
    );
  }
  return <SendChip {...props} />;
}

/** The separator the grammar is written with — `Queued · #2 in line · behind
 *  TASK-041 "…"`. A span rather than part of the caption string so the caption
 *  stays the one builder every surface shares (`platform/lib/queue`). */
function ChipDot() {
  return (
    <span className="sb-queued-dot" aria-hidden="true">
      ·
    </span>
  );
}

function SendChip({ send, busy, onSkip, onCancel, cancelling }: QueuedChipProps) {
  // `status` is asserted here rather than carried on the send: a chip only
  // exists because the server answered `run: false`, so "is this queued" is not
  // a question this component has — and queueLine's own guard stays the one
  // gate every other surface passes through.
  const line = queueLine({ ...send, status: "queued" });
  if (!line) return null;
  return (
    <div className="c-schedblock c-queuechip">
      {/* THE MESSAGE ITSELF, in the transcript's own user bubble — same class,
          same skin, same column (`.c-schedblock` and `.chat-log` share the
          720px measure and the 20px gutter), so it reads as the last row of the
          conversation rather than as a quotation of one. It is here rather than
          in the transcript because the transcript cannot keep it: see
          `QueuedSend.text`. */}
      {send.text ? (
        <div className="turn user c-queuesaid">
          <div className="bubble">{send.text}</div>
        </div>
      ) : null}
      {/* `role=status`: a message that did not start is news, and it has to
          reach a reader who is not looking at this corner of the pane —
          exactly the reason SchedBlock's own card carries one. */}
      <div className="sb-card sb-card--chip" role="status">
        <span className="sb-ring sb-ring--queued" aria-hidden="true" />
        {/* The WORD first, because it is the one that changes what the bubble
            above means: without it "runs next · behind TASK-041" reads as a
            note about somebody else's run. */}
        <span className="sb-queued-word">Queued</span>
        <ChipDot />
        <span className="sb-queued-line" title={line.aheadTitle || undefined}>
          {line.runsNext && (
            <span className="sb-queued-glyph" aria-hidden="true">
              {QUEUE_PRIORITY_GLYPH}
            </span>
          )}
          {line.text}
        </span>
        <span className="sb-grow" />
        {/* SKIP STAYS ON THE CHIP AT THE HEAD OF THE LINE, disabled. It is the
            control that put it there, and taking it away on the press that
            worked is how a reader ends up unsure whether anything happened.
            CANCEL SITS AFTER IT, quieter: this is the row's destructive verb and
            it is the second one read, not the first one reached. */}
        <span className="sb-acts sb-acts--chip">
          <button
            type="button"
            disabled={busy || cancelling || line.runsNext}
            title={
              line.runsNext
                ? "Already at the front of this folder's queue"
                : "Skip the queue — this runs next, nothing is interrupted"
            }
            onClick={onSkip}
          >
            Skip
          </button>
          {onCancel ? (
            <>
              <ChipDot />
              <button
                type="button"
                className="sb-quiet"
                disabled={busy || cancelling}
                title="Cancel this message — the words are dropped and nothing runs"
                onClick={onCancel}
              >
                Cancel
              </button>
            </>
          ) : null}
        </span>
      </div>
    </div>
  );
}

export default QueuedChip;
