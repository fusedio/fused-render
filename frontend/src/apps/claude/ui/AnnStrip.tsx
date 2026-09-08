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
// Comment and Annotate are PR3's. They are rendered DISABLED rather than
// omitted: the strip is Screenshot · Comment · Annotate in every state (Akshil,
// 2026-09-06), and a row that grows two buttons when the next PR lands is a row
// that moves under the reader's hand.
import "../styles/chat.css";

import { annotateLabelFor, shotLabelFor, type PaneNoun } from "../pane/paneUrl";

export interface AnnStripProps {
  /** "preview" for a file target, "app" for a project — every label names it. */
  paneNoun: PaneNoun;
  /** No pane to photograph: the buttons are HIDDEN, not disabled. The sidebar
   *  layout's case, where the target is the host's content pane and the host may
   *  be showing something unmarked — "absent beats dead" (T:238-241). */
  shown: boolean;
  /** A capture is in flight (`shotBusy`): the button says so by going inert, and
   *  the guard in the handler is the belt to that braces — a keyboard user can
   *  still reach a control a poll has not caught up with (T:11265). */
  capturing: boolean;
  onScreenshot(): void;
}

export function AnnStrip({ paneNoun, shown, capturing, onScreenshot }: AnnStripProps) {
  if (!shown) return null;
  return (
    <div className="c-anncta">
      <button
        type="button"
        className="c-viewshot"
        aria-label={shotLabelFor(paneNoun)}
        title={shotLabelFor(paneNoun)}
        disabled={capturing}
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
      {/* PR3 (inventory 02): the comment mode and the spoken walkthrough. The
          words and the glyphs are T's, so the seats are the right width the
          moment they come alive. */}
      <button
        type="button"
        aria-pressed="false"
        aria-label={annotateLabelFor(paneNoun)}
        title={"Comment on the " + paneNoun + ", then send the notes to Claude"}
        disabled
      >
        <svg className="c-cmt-bubble" viewBox="0 0 16 16" aria-hidden="true">
          <path d="M13.5 2.5h-11a1 1 0 0 0-1 1v7a1 1 0 0 0 1 1h2.5v2.9l3.4-2.9h5.1a1 1 0 0 0 1-1v-7a1 1 0 0 0-1-1z" />
        </svg>
        <span className="c-lbl">Comment</span>
      </button>
      <button
        type="button"
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
    </div>
  );
}

export default AnnStrip;
