// THE `#anncta` GROUP: the three controls that act on the PREVIEW rather than on
// the draft — Screenshot · Comment · Annotate (T:3975-4000 markup, CSS
// T:195-320).
//
// The camera was a pill beside Send in both composers and moved up here on
// 2026-08-27, because it acts on the pane like its two neighbours; the picture it
// takes still lands as a chip above the composer, where the message is. The
// order is Screenshot first (Akshil, 2026-09-04).
//
// WHERE the group lives is a layout question and the answer is the pane's
// (`pickerHost`, pane/LeftModePicker): the strip is the one row BOTH narrow views
// keep, and the wide layout's chat column keeps it too — so the group renders
// once, in the chat column's strip, and the narrow chat view HIDES the camera
// (T:3823 `body.view-chat .viewshot`) because the pane it photographs is not on
// screen there.
//
// PR3 gave Comment and Annotate their handlers. Three rules that are the whole
// of this file's own behaviour:
//
//   * ONE SEAT, THREE FACES for Comment (T:7695 `annBtnName`): the bubble arms
//     it, ✓ Done sends the round, and while a walkthrough records it is neither —
//     the stop is the bar's ■, not a neighbouring seat.
//   * ABSENT BEATS DEAD: with nothing to annotate all three seats are HIDDEN,
//     not disabled (T:238-241, 8447) — a dead row of three is worse than a row
//     that is not there.
//   * INERT IS SPOKEN, not only drawn (`seatsAria`, T:6816): the stylesheet dims
//     the camera and the mic while a comment round is armed
//     (`.c-anncta:has(.c-annbtn.on) .c-viewshot`, T:320) and `aria-disabled`
//     says the same thing to a reader who cannot see the dimming.
import "../styles/chat.css";

import {
  ANN_ARMED_TITLE,
  COMMENT_SEAT_WHILE_RECORDING,
  COMMENT_SEAT_WHILE_SETTLING,
  seatsAria,
  type AnnMode,
} from "../ann";
import { annotateLabelFor, shotLabelFor, type PaneNoun } from "../pane/paneUrl";

export interface AnnStripProps {
  /** "preview" for a file target, "app" for a project — every label names it. */
  paneNoun: PaneNoun;
  /** No pane at all: the buttons are HIDDEN, not disabled. The sidebar layout's
   *  case, where the target is the host's content pane and the host may be
   *  showing something unmarked — "absent beats dead" (T:238-241). */
  shown: boolean;
  /**
   * Whether the pane the CAMERA photographs is on screen. Its own flag and not
   * part of `shown`, because T's narrow rule takes exactly one seat away:
   * `body.view-chat .viewshot` (T:3823) parks the preview off screen, so there
   * is nothing to photograph — while Comment and Annotate act on a pane that is
   * still there, one toggle away, and are the reason that row is the ONE row the
   * narrow rules never hide.
   */
  cameraShown?: boolean;
  /** A capture is in flight (`shotBusy`): the button says so by going inert, and
   *  the guard in the handler is the belt to that braces — a keyboard user can
   *  still reach a control a poll has not caught up with (T:11265). */
  capturing: boolean;
  onScreenshot(): void;
  /** T:6122 `annCapable` — nothing to annotate: all three seats go, the camera
   *  included, because the pane it would photograph is the pane there is none
   *  of (T:8447). Defaults to `true`, so a host that knows nothing of the
   *  annotation subsystem still gets its camera. */
  capable?: boolean;
  /** The mode machine's one value (`useAnnotations().mode`). */
  mode?: AnnMode;
  /** T:8329 — the Comment seat: arm from rest, ✓ Done while armed, inert while a
   *  walkthrough records. Absent → the seat renders disabled, which is the shape
   *  a host without the subsystem gets. */
  onComment?(): void;
  /**
   * The Annotate seat, which is `ann/RecControls` — ONE control with four faces
   * (mic · pulsing stop · "Stopping…" · "Transcribing…") whose names and glyphs
   * belong to the recorder's own state machine and not to this row. Handed in as
   * a node rather than reimplemented here for exactly that reason: this file
   * owns WHERE the seat is, `RecControls` owns what it says.
   */
  recSeat?: React.ReactNode;
}

/** T:8114 `#anncta.busy` — a stop or a discard is in flight: the class the
 *  stylesheet's inert faces and its status label both hang off. */
function ctaClass(mode: AnnMode): string {
  const busy = mode === "settling" || mode === "transcribing";
  return "c-anncta" + (busy ? " busy" : "");
}

export function AnnStrip({
  paneNoun,
  shown,
  capturing,
  onScreenshot,
  cameraShown = true,
  capable = true,
  mode = "off",
  onComment,
  recSeat,
}: AnnStripProps) {
  if (!shown || !capable) return null;
  const aria = seatsAria(mode);
  const armed = mode !== "off";
  // T:7673 / 7701 — the seat's NAME, and while a walkthrough owns the mode the
  // recorder's own two names for it (`rec.ts`), so "one mode at a time" is
  // written once. The visible WORD is the static "Comment" in every state: a
  // label that changes width makes the whole right-anchored row shuffle on
  // every toggle (T:7689).
  const seat =
    mode === "recording"
      ? COMMENT_SEAT_WHILE_RECORDING
      : mode === "settling" || mode === "transcribing"
        ? COMMENT_SEAT_WHILE_SETTLING
        : mode === "comment"
          ? { label: "Done — send the notes and finish commenting", title: ANN_ARMED_TITLE }
          : {
              label: annotateLabelFor(paneNoun),
              title: "Comment on the " + paneNoun + ", then send the notes to Claude",
            };
  return (
    <div className={ctaClass(mode)}>
      {cameraShown ? (
      <button
        type="button"
        className="c-viewshot"
        aria-label={shotLabelFor(paneNoun)}
        title={shotLabelFor(paneNoun)}
        aria-disabled={aria.screenshot ? "true" : "false"}
        disabled={capturing || aria.screenshot}
        onClick={onScreenshot}
      >
        <svg viewBox="0 0 16 16" aria-hidden="true">
          <path
            d="M2.6 5.4h2.1l1-1.6h4.6l1 1.6h2.1a1 1 0 0 1 1 1v5.6a1 1 0 0 1-1 1H2.6a1 1 0 0 1-1-1V6.4a1 1 0 0 1 1-1Z"
            strokeLinejoin="round"
          />
          <circle cx="8" cy="9" r="2.4" />
        </svg>
        <span className="c-lbl">Screenshot</span>
      </button>
      ) : null}
      <button
        type="button"
        className={"c-annbtn" + (armed ? " on" : "")}
        aria-pressed={armed ? "true" : "false"}
        aria-label={seat.label}
        aria-disabled={aria.comment ? "true" : "false"}
        title={seat.title}
        disabled={!onComment || aria.comment}
        onClick={onComment}
      >
        {/* All three glyphs are in the markup and the stylesheet picks one off
            `.on` / the row's `:has(.c-annrec.on)`, exactly as T does: a seat
            whose glyph is swapped in JS re-lays the row out on every transition
            (T:3992). */}
        <svg className="c-cmt-bubble" viewBox="0 0 16 16" aria-hidden="true">
          <path d="M13.5 2.5h-11a1 1 0 0 0-1 1v7a1 1 0 0 0 1 1h2.5v2.9l3.4-2.9h5.1a1 1 0 0 0 1-1v-7a1 1 0 0 0-1-1z" />
        </svg>
        <svg className="c-cmt-done" viewBox="0 0 16 16" aria-hidden="true">
          <path d="M2.5 8.5l3.5 3.5 7-8" />
        </svg>
        <span className="c-lbl c-cmt-word">Comment</span>
        <span className="c-lbl c-done-word">Done</span>
      </button>
      {recSeat ?? (
        // The shape a host without the recorder gets. The seat is in the row in
        // every state (Akshil, 2026-09-06), so it renders dead rather than
        // letting the row grow a button later under the reader's hand.
        <button
          type="button"
          className="c-annrec"
          aria-pressed="false"
          aria-label="Annotate with a spoken walkthrough"
          title="Annotate with a spoken walkthrough — talk while you click, and each click becomes a note"
          disabled
        >
          <svg className="c-rec-mic" viewBox="0 0 16 16" aria-hidden="true">
            <rect x="6" y="1.5" width="4" height="7.5" rx="2" />
            <path d="M3.5 7.5a4.5 4.5 0 0 0 9 0" />
            <line x1="8" y1="12" x2="8" y2="14.5" />
          </svg>
          <span className="c-lbl">Annotate</span>
        </button>
      )}
    </div>
  );
}

export default AnnStrip;
