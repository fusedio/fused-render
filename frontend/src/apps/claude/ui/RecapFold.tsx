// The "Recap:" line — Claude Code's own, copied as it draws it in the terminal
// (2.1.268, `RecapFoldMessage`):
//
//     ※ recap: Building a "While you were away" fold; it's built, browser-
//              verified PASS. Next: run the test plan.
//
// A LINE IN THE LOG, not a card and not a control. It sits after the last turn
// inside `.chat-log` (Transcript renders it), scrolls with the conversation and
// takes the muted one-liner shape of the `note` turns beside it ("Interrupted
// by you") at the transcript's reading size. Glyph, bold "Recap:", italic body.
// Nothing to click and nothing to dismiss: it goes away on its own when the
// reader sends the next message (useAwayRecap), the way the terminal's line
// scrolls off under the next turn.
export interface RecapFoldProps {
  text: string;
}

export function RecapFold({ text }: RecapFoldProps) {
  return (
    <div className="turn note recap" data-recap="">
      <span className="eye" aria-hidden="true">
        ※
      </span>
      <b className="recap-label">Recap:</b>
      <span className="recap-text">{text}</span>
    </div>
  );
}

export default RecapFold;
