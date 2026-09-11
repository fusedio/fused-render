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
// once, in the chat column's strip, and the narrow CHAT view hides the two seats
// whose pane is parked off screen there: the camera (T:3823 `body.view-chat
// .viewshot`, nothing to photograph) and Comment (T:3822 `body.view-chat #annbtn`,
// nothing to pin onto). Annotate stays — a spoken walkthrough is about the app
// the reader is describing, not about what is currently on screen.
//
// PR3 gave Comment and Annotate their handlers. Three rules that are the whole
// of this file's own behaviour:
//
//   * ONE SEAT, THREE SPOKEN NAMES for Comment and exactly ONE DRAWING
//     (T:7695 `annBtnName`): the `aria-label`/`title` become the arm, the ✓ Done
//     that sends the round, and the resting name a walkthrough leaves it with —
//     while the glyph and the visible word never change, because a seat that
//     re-widths shuffles this whole right-anchored row on every toggle (T:7689).
//     The accent fill says armed; the way OUT is the bar's ■/✓, never a
//     neighbouring seat.
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
import {
  annIdleTitleFor,
  annotateLabelFor,
  shotLabelFor,
  type PaneNoun,
} from "../pane/paneUrl";

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
  /**
   * Whether the pane the COMMENT seat pins onto is on screen. T's narrow rule
   * is the camera's twin — `body.view-chat #annbtn { display: none }` (T:3822)
   * — and for the same reason read one step further: with the preview parked off
   * screen there is nothing to arm against, and arming anyway puts the framed
   * document's capture-phase click swallower live over a pane the reader cannot
   * see. `useNarrowView`'s `onArriveChat` disarms on ARRIVAL; only absence
   * stops a FRESH arm afterwards, keyboard included.
   *
   * A prop rather than a CSS rule because this file owns which seats show, and
   * `useFitStrip` then measures a node set that is stable for the layout it is
   * folding.
   */
  commentShown?: boolean;
  /** A capture is in flight (`shotBusy`): the button says so by going inert, and
   *  the guard in the handler is the belt to that braces — a keyboard user can
   *  still reach a control a poll has not caught up with (T:11265). */
  capturing: boolean;
  /**
   * A PENDING SCHEDULED MESSAGE HOLDS THIS CHAT (P4R1-2, Akshil, 2026-09-10:
   * "Screenshot/Comment/Annotate seats disabled too while blocked").
   *
   * All three seats END IN THE COMPOSER — a picture lands as a chip above the
   * box, a comment round and a walkthrough both send their notes through it —
   * and the composer is shut for as long as the block holds. So a live seat here
   * is an invitation to gather work that has nowhere to go; the reason is on the
   * banner directly below, which is why these carry no second wording of it.
   *
   * DISABLED, not hidden. "Absent beats dead" is this file's rule for a seat
   * with no PANE to act on (`shown`, `capable`) — a permanent fact about the
   * host. A block is a wait, measured in minutes, and a row that loses three
   * buttons and grows them back is a row that moved under the reader's hand.
   */
  blocked?: boolean;
  onScreenshot(): void;
  /** T:6122 `annCapable` — nothing to annotate: all three seats go, the camera
   *  included, because the pane it would photograph is the pane there is none
   *  of (T:8447). Defaults to `true`, so a host that knows nothing of the
   *  annotation subsystem still gets its camera. */
  capable?: boolean;
  /** The mode machine's one value (`useAnnotations().mode`). */
  mode?: AnnMode;
  /** `annOn` (`useAnnotations().armed`) — the reader's mode, as against the
   *  recorder's phase. Defaults to `mode !== "off"`, which is right in every
   *  state but an Esc'd settle; see the derivation in the body. */
  armed?: boolean;
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
  blocked = false,
  onScreenshot,
  cameraShown = true,
  commentShown = true,
  capable = true,
  mode = "off",
  armed: armedProp,
  onComment,
  recSeat,
}: AnnStripProps) {
  if (!shown || !capable) return null;
  // `annOn`, which is NOT `mode !== "off"` in one window: Esc during
  // Stopping…/Transcribing… takes the reader out of the mode while the recorder
  // goes on settling, so `mode` still reads "transcribing". T:7653-7660 hangs
  // `.on`, `aria-pressed` and `disabled` off `annOn` for exactly that reason —
  // the seat belongs to the reader's mode, not to the recorder's phase. The
  // fallback keeps a host that hands us only a mode on the old derivation.
  const armed = armedProp ?? mode !== "off";
  const aria = seatsAria(mode, armed);
  // T:7673 / 7701 — the seat's NAME, and while a walkthrough owns the mode the
  // recorder's own two names for it (`rec.ts`), so "one mode at a time" is
  // written once. The visible WORD is the static "Comment" in every state: a
  // label that changes width makes the whole right-anchored row shuffle on
  // every toggle (T:7689).
  //
  // The recorder's two names are gated on `armed` for the same reason as the
  // rest: once the reader has Esc'd out, the settle is the ANNOTATE seat's
  // status to carry and this seat is idle again, so it says so (T:7660's
  // `annSetMode(false)` restores `annIdleTitle()` there).
  const seat =
    armed && mode === "recording"
      ? COMMENT_SEAT_WHILE_RECORDING
      : armed && (mode === "settling" || mode === "transcribing")
        ? COMMENT_SEAT_WHILE_SETTLING
        : armed && mode === "comment"
          ? { label: "Done — send the notes and finish commenting", title: ANN_ARMED_TITLE }
          : {
              label: annotateLabelFor(paneNoun),
              // The shared helper, not a literal: T extracted `annIdleTitle`
              // (T:7505-7508) precisely because two writers spelled this
              // sentence out and the first disarm threw the kind-correct noun
              // away. Its armed twin is already an export; this one now is too.
              title: annIdleTitleFor(paneNoun),
            };
  return (
    <div className={ctaClass(mode)}>
      {cameraShown ? (
      <button
        type="button"
        className="c-viewshot"
        aria-label={shotLabelFor(paneNoun)}
        title={shotLabelFor(paneNoun)}
        aria-disabled={aria.screenshot || blocked ? "true" : "false"}
        disabled={capturing || aria.screenshot || blocked}
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
      {commentShown ? (
      <button
        type="button"
        className={"c-annbtn" + (armed ? " on" : "")}
        aria-pressed={armed ? "true" : "false"}
        aria-label={seat.label}
        aria-disabled={aria.comment || blocked ? "true" : "false"}
        title={seat.title}
        disabled={!onComment || aria.comment || blocked}
        onClick={onComment}
      >
        {/* Both glyphs and both words are in the markup and NEITHER SPARE IS
            EVER SHOWN — T:296 hides the check and "Done" with no `.on`
            override anywhere, because a seat whose glyph or label is swapped
            re-lays the row out on every transition (T:3992, T:7689), and this
            row is right-anchored beside the ⋮. The seat's SPOKEN name does
            change (`seat.label` above, T:7695 `annBtnName`); its drawing does
            not, and the accent fill is the mode's one visible signal. The
            spare nodes stay for T's own reason: baked-in markup the stylesheet
            picks from, never a JS glyph swap. */}
        <svg className="c-cmt-bubble" viewBox="0 0 16 16" aria-hidden="true">
          <path d="M13.5 2.5h-11a1 1 0 0 0-1 1v7a1 1 0 0 0 1 1h2.5v2.9l3.4-2.9h5.1a1 1 0 0 0 1-1v-7a1 1 0 0 0-1-1z" />
        </svg>
        <svg className="c-cmt-done" viewBox="0 0 16 16" aria-hidden="true">
          <path d="M2.5 8.5l3.5 3.5 7-8" />
        </svg>
        <span className="c-lbl c-cmt-word">Comment</span>
        <span className="c-lbl c-done-word">Done</span>
      </button>
      ) : null}
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
