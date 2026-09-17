// A MESSAGE THIS CHAT HAS NOT SENT YET, drawn as what it is: the reader's own
// bubble, at its place in the conversation, with a dashed edge where a sent one
// has a filled ground (the project queue, prefs `queue.enabled`).
//
// WHAT IT REPLACES. Until 2026-09-12 this was a CHIP — a card with a ring, a
// status word, a place ("#2 in line"), a Skip and a Cancel, parked under the
// transcript. It said everything and belonged to nothing: a second kind of object
// in a column of bubbles, describing a message that was sitting right above it in
// a bubble of its own. The reader had to pair the two up before either meant
// anything, and on a narrow pane the card's own row of controls squeezed the
// sentence it existed to say.
//
// THE SHAPE NOW IS THE TRANSCRIPT'S OWN. The message is a user bubble, because it
// is a user message; it is TRANSPARENT WITH A DASHED BORDER, because it has not
// happened yet; and the one thing a bubble cannot say — that it is waiting, what
// for, and how to take it back — is one muted line underneath, in the smallest
// register this pane has.
//
// NOT FADED. A dashed edge says "not yet"; dimmed text says "less important", and
// these are the reader's own words, which are not. The distinction was made
// explicitly (Akshil, 2026-09-12) and it is why the bubble keeps full-strength
// ink while the LINE under it is the muted one.
import "../styles/sched.css";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  canRunNext,
  queueAheadHref,
  queueBehind,
  queueRunsNext,
  RUN_NEXT_HINT,
  RUN_NEXT_LABEL,
  waitingCardText,
  waitingCount,
  NEXT_IN_FOLDER,
} from "@platform/lib/queue";
import type { QueueFacts } from "@platform/lib/queue";
import {
  WAITING_DELETE,
  WAITING_SKIP,
  WAITING_STOP,
  WAITING_STOP_ARMED,
  waitingWhen,
} from "../sched/waiting";
import type { WaitingRowData } from "../sched/waiting";
import { useDismissOnWindow } from "./useDismissOnWindow";

/** The separator the line is written with — `queued · behind TASK-038 · delete`.
 *  A span rather than part of a string so each piece can be its own element (one
 *  of them is a link, another a button) while the sentence still reads as one.
 *  Decorative: a screen reader gets the words. */
function Dot() {
  return (
    <span className="c-waiting-dot" aria-hidden="true">
      ·
    </span>
  );
}

export interface WaitingRowProps {
  row: WaitingRowData;
  /** The folder's answer, shared by every row in this chat — see
   *  `sched/waiting.waitingFacts` for why it is one answer and not one per row. */
  facts: QueueFacts;
  /** The clock the `scheduled` row's time is read against. Injected so a suite
   *  is not writing tests against the wall. */
  now?: Date;
  /** A delete is in flight: the one control is dead for its duration, and it is
   *  dead rather than gone — a control that disappears on the press that worked
   *  is how a reader ends up unsure anything happened. */
  deleting?: boolean;
  onDelete(): void;
  /** STOP THE REPEAT ITSELF — only ever called for a `row.repeat` row, and only
   *  on the second press. Absent means the row offers no such action, which is
   *  every ordinary message. */
  onStopRepeat?(): void;
}

export function WaitingRow({
  row,
  facts,
  now,
  deleting,
  onDelete,
  onStopRepeat,
}: WaitingRowProps) {
  const behind = queueBehind(facts);
  const href = queueAheadHref(facts);
  const when = row.word === "scheduled" ? waitingWhen(row.due, now) : "";
  /**
   * A CLAIMED MESSAGE HAS NOTHING LEFT TO TAKE BACK. `starting` is the second
   * between the scheduler taking the entry and the turn appearing above this
   * row: the words are already on their way, so a `delete` there would be a
   * button that answers 404 — and the row is drawn at all (rather than blinking
   * out until the turn lands) for exactly that reason.
   */
  const canAct = row.word !== "starting";
  const repeat = row.repeat && !!row.stopId && !!onStopRepeat;
  /**
   * TWO PRESSES, AND ONLY FOR THE REPEAT — the banner's own rule, kept where the
   * banner no longer is: under the flag the block draws nothing, so this row is
   * the only place a repeating message can be stopped from inside the chat.
   * `delete` beside it is one press, because it spends ONE run whose words are
   * in the bubble directly above it.
   */
  const [armed, setArmed] = useState(false);
  const stopRef = useRef<HTMLButtonElement | null>(null);
  const disarm = useCallback(() => setArmed(false), []);
  useDismissOnWindow(armed, disarm);
  // The arm belongs to the message it was made against.
  useEffect(() => setArmed(false), [row.entryId]);
  // The way back out is the gesture — a press outside, or Escape — exactly as
  // the banner's stop teaches (`useSchedule`, T:17353-17377). `contains` is asked
  // of the button directly and with no `instanceof Node` guard in front of it:
  // `Node` is a DOM global, and in a document that does not define one the guard
  // THROWS inside the listener and takes the disarm with it.
  useEffect(() => {
    if (!armed) return;
    const onDown = (ev: PointerEvent) => {
      const btn = stopRef.current;
      if (btn && ev.target && btn.contains(ev.target as Node)) return;
      setArmed(false);
    };
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key !== "Escape") return;
      ev.stopPropagation();
      ev.preventDefault();
      setArmed(false);
    };
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey, true);
    };
  }, [armed]);
  return (
    <div className="c-waiting" data-entry={row.entryId}>
      {/* THE MESSAGE ITSELF, in the transcript's own user bubble — same element,
          same class, same column (`.c-waiting` shares the log's 720px measure and
          20px gutter), so it lands where the turns above it land and reads as the
          next one of them rather than as a quotation of one.

          NO BUBBLE FOR A WORDLESS SEND. Pictures or notes alone have no typed
          line, and their real bubble is markers only the controller can compose;
          the line below still says the message is waiting, which is the fact that
          would otherwise be lost. */}
      {row.text ? (
        <div className="turn user c-waiting-turn">
          <div className="bubble c-waiting-bubble">{row.text}</div>
        </div>
      ) : null}
      {/* `role=status`: a message that did not start is news, and it has to reach
          a reader who is not looking at this corner of the pane. */}
      <p className="c-waiting-line" role="status">
        <span className="c-waiting-word">{row.word}</span>
        {/* BEFORE THE DUE TIME, WHEN; AFTER IT, WHAT IS IN THE WAY. Two different
            questions, and only one of them has an answer at any moment — see
            `sched/waiting.waitingLine`, which is the same branch in words. */}
        {row.word === "scheduled" && when ? (
          <>
            <Dot />
            <span className="c-waiting-when">{when}</span>
          </>
        ) : null}
        {row.word === "queued" && behind ? (
          <>
            <Dot />
            <span className="c-waiting-behind">
              {"behind "}
              {/* THE ID IS THE LINK, not the whole phrase: "behind" is this
                  sentence's own word and TASK-038 is the other conversation. The
                  title carries what that task is ABOUT, which is the thing a
                  reader wants and the thing that used to be spent as ink inside
                  the caption. A holder with no session yet has nowhere to go, and
                  the id is then plain text rather than a link to nothing. */}
              {href ? (
                <a
                  className="c-waiting-ahead"
                  href={href}
                  title={facts.queue_ahead_title || undefined}
                >
                  {facts.queue_ahead}
                </a>
              ) : (
                <span className="c-waiting-ahead" title={facts.queue_ahead_title || undefined}>
                  {facts.queue_ahead}
                </span>
              )}
            </span>
          </>
        ) : null}
        {/* TAKE IT BACK. One press and no arming: this drops ONE message, whose
            words are in the bubble directly above the button — a repeat's stop
            spends every future run and has to be confirmed, and this does not.
            Lower case, no border, in the line's own register: it is a verb in a
            sentence, not a button bolted to the end of one.

            AND ITS WORD CHANGES ON A REPEAT'S OCCURRENCE, because the act does:
            the template arms the next run the moment this one is skipped, so
            `delete` promised something it does not do. */}
        {canAct ? (
          <>
            <Dot />
            <button
              type="button"
              className="c-waiting-del"
              disabled={deleting}
              title={
                repeat
                  ? "Skip this run — the next one is still scheduled"
                  : "Delete this message — the words are dropped and nothing runs"
              }
              onClick={onDelete}
            >
              {repeat ? WAITING_SKIP : WAITING_DELETE}
            </button>
          </>
        ) : null}
        {/* …AND THE ONE THAT STOPS IT COMING BACK. Armed, because it spends every
            run this message would ever have made and nothing on this page can
            restore them; the label it arms into names that loss. */}
        {canAct && repeat ? (
          <>
            <Dot />
            <button
              type="button"
              ref={stopRef}
              className="c-waiting-del c-waiting-stop"
              disabled={deleting}
              aria-pressed={armed}
              title="Stops the repeating message — no further runs are scheduled"
              onClick={() => {
                if (!armed) {
                  setArmed(true);
                  return;
                }
                setArmed(false);
                onStopRepeat?.();
              }}
            >
              {armed ? WAITING_STOP_ARMED : WAITING_STOP}
            </button>
          </>
        ) : null}
      </p>
    </div>
  );
}

export interface WaitingCardProps {
  /** How many messages of this conversation's are waiting. Never 0 — the caller
   *  draws nothing at all then, because "0 messages waiting" is a card about
   *  nothing sitting on top of the box. */
  count: number;
  facts: QueueFacts;
  /** Run next is in flight, or already spent: the button is dead either way. */
  busy?: boolean;
  onRunNext(): void;
}

/**
 * THE ONE CARD OVER THE COMPOSER — "2 messages waiting · behind TASK-038", and
 * the one press that changes it.
 *
 * WHY IT EXISTS BESIDE THE ROWS. The rows are in the transcript, which scrolls;
 * a reader who has scrolled up, or whose chat has twenty turns above the waiting
 * ones, has no idea anything is held. This is the summary, pinned where the
 * composer is, and it is deliberately a SUMMARY: one count, one thing in front,
 * one verb. Everything per-message (the words, the delete) is on the row.
 *
 * RUN NEXT ONLY WHEN ANOTHER WAITING TASK IS AHEAD (`canRunNext`, and its note
 * carries the why). The press can never touch the run holding the folder, so at
 * the head of the line there is nothing for it to get in front of — and a
 * control whose only possible outcome is the state you are already in is a
 * control that teaches the reader it does nothing.
 *
 * THE SENTENCE IS UNAFFECTED. `behind TASK-056` stays printed at the head of the
 * line, because it is true there; only `queue_priority` — the spot actually
 * claimed — replaces it with "next in this folder".
 */
export function WaitingCard({ count, facts, busy, onRunNext }: WaitingCardProps) {
  if (count <= 0) return null;
  /**
   * WHAT IS IN FRONT is a different question from WHETHER THERE IS A PRESS, and
   * conflating them was the bug (Akshil, 2026-09-12). `1 message waiting ·
   * behind TASK-056` is TRUE at the head of the line — the run holding the
   * folder is genuinely in front — and the card used to swap it for "next in
   * this folder" purely because no button was going to be drawn. So the sentence
   * now follows `waitingCardText`'s own rule (`queue_priority` alone silences
   * it) and the button follows `canRunNext`.
   */
  const behind = queueRunsNext(facts) ? "" : queueBehind(facts);
  const showRun = canRunNext(facts);
  const href = queueAheadHref(facts);
  return (
    <div className="c-waitcard">
      <div className="wc-card" role="status" aria-label={waitingCardText(count, facts)}>
        {/* The ring the whole state is coloured by — the running yellow, dashed,
            the same mark the rows and the Tasks lane wear. `aria-hidden`: the card's own label
            already says everything it stands for. */}
        <span className="wc-ring" aria-hidden="true" />
        <span className="wc-text">
          <span className="wc-count">{waitingCount(count)}</span>
          <span className="wc-dot" aria-hidden="true">
            ·
          </span>
          {behind ? (
            <span className="wc-behind">
              {"behind "}
              {href ? (
                <a className="wc-ahead" href={href} title={facts.queue_ahead_title || undefined}>
                  {facts.queue_ahead}
                </a>
              ) : (
                <span className="wc-ahead" title={facts.queue_ahead_title || undefined}>
                  {facts.queue_ahead}
                </span>
              )}
            </span>
          ) : (
            <span className="wc-behind">{NEXT_IN_FOLDER}</span>
          )}
        </span>
        <span className="wc-grow" />
        {/* RUN NEXT ONLY WHEN ANOTHER WAITING TASK IS AHEAD (`canRunNext`).
            Behind the folder's own holder there is nothing for this press to get
            in front of — it never interrupts a run — and a control whose only
            outcome is the state you are already in teaches the reader that it
            does nothing. */}
        {showRun ? (
          <button type="button" className="wc-run" disabled={busy} title={RUN_NEXT_HINT} onClick={onRunNext}>
            {RUN_NEXT_LABEL}
          </button>
        ) : null}
      </div>
    </div>
  );
}

export default WaitingRow;
