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
// a ring, the words, and Skip.
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
  send: QueuedSend;
  /** Skip is in flight, or already spent: the button is dead either way, and it
   *  is dead rather than gone for the reason the Tasks row's is (see there). */
  busy?: boolean;
  /** Send this to the front of its folder's line. Never offered at the head. */
  onSkip(): void;
}

export function QueuedChip({ send, busy, onSkip }: QueuedChipProps) {
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
            worked is how a reader ends up unsure whether anything happened. */}
        <span className="sb-acts">
          <button
            type="button"
            disabled={busy || line.runsNext}
            title={
              line.runsNext
                ? "Already at the front of this folder's queue"
                : "Skip the queue — this runs next, nothing is interrupted"
            }
            onClick={onSkip}
          >
            Skip
          </button>
        </span>
      </div>
    </div>
  );
}

export default QueuedChip;
