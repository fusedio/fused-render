// The heads-up card: one short note the side check found worth interrupting for.
//
// Drawn in the transcript's recap slot (ClaudeChat's `recap` prop), ahead of
// the recap line. Not a permission card: nothing is blocked on it, so it takes
// the quiet border rather than the accent ring.
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
      <div className="headsup-tag">{note.tag}</div>
      <div className="headsup-title">{note.title}</div>
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
