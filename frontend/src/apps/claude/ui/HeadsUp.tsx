// The heads-up note: one short suggestion from the side check.
//
// Drawn in the transcript's recap slot (ClaudeChat's `recap` prop), ahead of
// the recap line, in the recap's register: an aside with a faint left rule
// only, no plate and no accent, and its actions as plain text links. Nothing
// is blocked on it. `warn` stays on the root as a hook but carries no styling.
import type { HeadsUpNote } from "./useHeadsUp";
import { MarkdownView } from "./MarkdownView";

export interface HeadsUpProps {
  note: HeadsUpNote | null;
  onGotIt: () => void;
  onAsk: () => void;
  onDismiss: () => void;
}

export function HeadsUp({ note, onGotIt, onAsk, onDismiss }: HeadsUpProps) {
  if (!note) return null;
  return (
    <div className={"headsup" + (note.tag === "Heads up" ? " warn" : "")} data-headsup="" role="note">
      <div className="headsup-head">
        <span className="headsup-tag">{note.tag}</span>
        <span className="headsup-title">{note.title}</span>
      </div>
      <ul className="headsup-bullets">
        {note.bullets.map((b, i) => (
          <li key={i}>
            <MarkdownView text={b} className="headsup-bullet" enhance={false} links={false} />
          </li>
        ))}
      </ul>
      <div className="headsup-actions">
        <button type="button" className="headsup-btn primary" onClick={onGotIt}>
          Got it
        </button>
        <button type="button" className="headsup-btn" onClick={onAsk}>
          Ask in chat
        </button>
        <button type="button" className="headsup-btn" onClick={onDismiss}>
          Dismiss
        </button>
      </div>
    </div>
  );
}

export default HeadsUp;
