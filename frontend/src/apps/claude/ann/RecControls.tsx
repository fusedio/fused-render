// THE RECORD AFFORDANCE — `#annrec` + `#annreclbl` (T:3993-3995) and the bar's
// ■/clock/trash (T:6221-6240), as one component.
//
// It is ONE control with four faces, not four controls, because that is what
// the walkthrough actually is: the mic starts it, the same seat stops it (the
// pulsing seat IS the live control, T:8329-8331), and through the settle it
// becomes a STATUS rather than a button — a disabled seat named for what is
// happening. T learned that the hard way (Bugbot, PR #665): with the recording
// flag gone but the armed CSS still on, the seat showed an enabled ✓ Done for
// the width of the stop, and a click there disarmed the mode before
// transcription ever started (T:8167-8177).
//
// Every string here is T's, verbatim, with its line. The GLYPHS are T's too
// (T:3995 mic, T:6225 ■, T:6244 trash) so the seat is the right width the
// moment it comes alive.
//
// Presentational only: no store, no capture, no timers. `ann/rec.ts` owns the
// state and the clock; the AnnBar passes a snapshot and three callbacks.
import {
  COMMENT_SEAT_WHILE_RECORDING,
  COMMENT_SEAT_WHILE_SETTLING,
  recSeatName,
  type RecSnapshot,
  type SeatName,
} from "./rec";

export interface RecControlsProps {
  /** `createRecorder(...).snapshot()` — the state, the clock text, the count. */
  rec: RecSnapshot;
  /** No pane to annotate: the control is HIDDEN, not disabled — "absent beats
   *  dead", the same rule AnnStrip follows for the camera (T:238-241). */
  shown?: boolean;
  /** `annOn && !annRecOn` — a TYPED comment mode is armed, so the mic is inert:
   *  one mode at a time, leave it first (2026-09-06, T:8329-8331). */
  commentArmed?: boolean;
  /** Whether to draw the trash beside the stop. The bar hides itself through
   *  Stopping…/Transcribing… (`annBarPaint`'s busy gate, T:6233-6235), so the
   *  recording's waiting marks can never be thrown from here — which is why
   *  this component renders it only while `state === "recording"` regardless. */
  discardable?: boolean;
  onBegin(): void;
  onEnd(): void;
  onDiscard(): void;
}

const MicGlyph = () => (
  <svg className="c-rec-mic" viewBox="0 0 16 16" aria-hidden="true">
    <rect x="6" y="1.5" width="4" height="7.5" rx="2" />
    <path d="M3.5 7.5a4.5 4.5 0 0 0 9 0" />
    <line x1="8" y1="12" x2="8" y2="14.5" />
  </svg>
);

const StopGlyph = () => (
  <svg className="c-rec-stop" viewBox="0 0 16 16" aria-hidden="true">
    <rect x="4" y="4" width="8" height="8" rx="1.5" />
  </svg>
);

const TrashGlyph = () => (
  <svg viewBox="0 0 16 16" aria-hidden="true">
    <path d="M2.5 4h11" />
    <path d="M5.5 4V2.75a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1V4" />
    <path d="M3.75 4l.6 9a1 1 0 0 0 1 .95h5.3a1 1 0 0 0 1-.95l.6-9" />
    <line x1="6.5" y1="7" x2="6.5" y2="10.5" />
    <line x1="9.5" y1="7" x2="9.5" y2="10.5" />
  </svg>
);

/** The Comment seat's name while a walkthrough owns the mode — exported for
 *  whoever draws that seat (AnnBar), so the two halves of "one mode at a time"
 *  are written once (T:7936-7937, 8171-8172). */
export function commentSeatName(rec: RecSnapshot): SeatName | null {
  if (rec.state === "recording" || rec.state === "starting") {
    return COMMENT_SEAT_WHILE_RECORDING;
  }
  if (rec.busy) return COMMENT_SEAT_WHILE_SETTLING;
  return null;
}

export function RecControls({
  rec,
  shown = true,
  commentArmed = false,
  discardable = true,
  onBegin,
  onEnd,
  onDiscard,
}: RecControlsProps) {
  if (!shown) return null;
  const seat = recSeatName(rec.state);
  const live = rec.state === "recording";
  // Disabled for the width of the START request too, not just while a
  // recording runs — otherwise a second click before the app answers the first
  // one opens two recordings (T:7868-7871). Through the settle the seat is a
  // status, and while a typed comment mode is armed it is inert.
  // `cancelling` counts as well — a dismissed start being put back down. The
  // mode is already handed back by then, so nothing else marks the seat
  // unavailable, and `begin()` refuses for the width of that teardown: a press
  // inside it would be a dead click (Bugbot, PR #1074).
  const inert =
    rec.state === "starting" ||
    rec.state === "cancelling" ||
    rec.busy ||
    (!live && commentArmed);
  return (
    <>
      <button
        type="button"
        className={"c-annrec" + (live ? " on" : "")}
        aria-pressed={live ? "true" : "false"}
        aria-disabled={inert ? "true" : "false"}
        aria-label={seat.label}
        title={seat.title}
        disabled={inert}
        onClick={live ? onEnd : onBegin}
      >
        {live ? <StopGlyph /> : <MicGlyph />}
        {/* The resting word, hidden by the stylesheet through a settle
            (`#anncta.busy #annrec .rec-word`, T:312) — the status takes the
            space. */}
        {!live && !rec.busy ? <span className="c-lbl c-rec-word">Annotate</span> : null}
        {/* ONE label for the clock and the three settle tenses, because they
            are one fact in different tenses and `#annreclbl` is one node
            (T:3995, 7797-7803). Empty at rest, and empty renders nothing —
            `#annreclbl:empty` is hidden (T:324). */}
        {rec.status ? <span className="c-annreclbl">{rec.status}</span> : null}
      </button>
      {live && discardable ? (
        <button
          type="button"
          className="c-annrec-discard"
          aria-label="Discard the recording"
          title="Discard the recording — nothing is transcribed or sent"
          onClick={onDiscard}
        >
          <TrashGlyph />
        </button>
      ) : null}
    </>
  );
}

export default RecControls;
