// THE QUEUED LINES, above the composer: what was typed while a turn runs and
// the CLI has not echoed yet, listed the way Claude Code lists them. They are
// not transcript bubbles; each becomes one when the CLI echoes it. One row per
// line, with its send-now affordance and, for a line this page still holds in
// its outbox, the way back into the box.
import type { QueuedLine } from "../protocol/queued-lines";

export interface QueuedStripProps {
  lines: readonly QueuedLine[];
  /** A turn is running: a line can be sent now (Ctrl+X), interrupting it. */
  running: boolean;
  onSendNow: () => void;
  /** Put an outbox line back into the box, keyed by line id. */
  onPull: (id: string) => void;
  /** A card tile: read-only. The lines are listed, with no send-now or edit. */
  compact?: boolean;
}

export function QueuedStrip({ lines, running, onSendNow, onPull, compact }: QueuedStripProps) {
  if (!lines.length) return null;
  return (
    <ul className={compact ? "c-queue-strip c-queue-compact" : "c-queue-strip"} aria-label="Queued messages">
      {lines.map((line) => (
        <li className="c-queue-row" key={line.id}>
          <span className="c-queue-text">{line.text}</span>
          {compact ? null : line.pending === "held" ? (
            <span className="c-queue-note">(queued until this turn is done)</span>
          ) : running ? (
            <button
              type="button"
              className="c-queue-act"
              title="Waiting for the current turn. Click to interrupt it and send now."
              onClick={onSendNow}
            >
              (Ctrl+X to send now)
            </button>
          ) : null}
          {!compact && line.pending ? (
            <button
              type="button"
              className="c-queue-act"
              title="Put this line back in the box."
              onClick={() => onPull(line.id)}
            >
              edit
            </button>
          ) : null}
        </li>
      ))}
    </ul>
  );
}
