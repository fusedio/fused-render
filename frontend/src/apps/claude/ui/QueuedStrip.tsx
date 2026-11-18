// THE QUEUED LINES, above the composer: what was typed while a turn runs and
// the CLI has not echoed yet, listed the way Claude Code lists them. They are
// not transcript bubbles; each becomes one when the CLI echoes it. One row per
// line, with its send-now affordance and, for a line this page still holds in
// its outbox, the way back into the box.
import { useEffect, useRef, useState } from "react";
import type { QueuedLine } from "../protocol/queued-lines";

/** How long a line that just joined the queue wears its arrival class — the
 *  length of the CSS arrival (styles/sched.css `c-queue-glow`, 1800ms) and a
 *  little over, so the class never comes off a frame early. */
export const ARRIVE_FLASH_MS = 1900;

const NONE: ReadonlySet<string> = new Set();

/**
 * WHICH LINES JUST ARRIVED — the ids that joined `lines` while this strip was
 * on screen, each for `ARRIVE_FLASH_MS`.
 *
 * NOT "EVERY ROW THAT MOUNTS". The strip mounts with its chat, and a chat is
 * mounted over and over with the same waiting lines in it: a rail card opened
 * on hover, a pane switched back to, a card closed over the composer. A flash
 * tied to the row's mere appearance said "your message just went here" about
 * lines that had been waiting for minutes (Bugbot and the review on PR #2347).
 * So what is there at mount is already known, and only an id seen for the first
 * time AFTER that is an arrival.
 */
function useArrivals(lines: readonly QueuedLine[], ready: boolean): ReadonlySet<string> {
  const known = useRef<Set<string> | null>(null);
  const timers = useRef(new Set<ReturnType<typeof setTimeout>>());
  const [arriving, setArriving] = useState<ReadonlySet<string>>(NONE);
  // The ids as ONE string for the effect's dependency. JSON, not a joined list:
  // an inbox row with no id of its own is keyed by its words, and words have
  // newlines in them (CodeRabbit, PR #2347).
  const ids = JSON.stringify(lines.map((l) => l.id));
  useEffect(() => {
    const now = JSON.parse(ids) as string[];
    const before = known.current;
    known.current = new Set(now);
    // NOT YET, OR ONLY JUST: while the chat is still reading its history the
    // strip is empty for a reason that is not "nothing is queued", and the
    // lines the restore brings are the ones that were waiting all along. They
    // are the baseline, not arrivals (CodeRabbit, PR #2347) — so the first
    // look, and every look until the chat has settled, only remembers.
    if (before === null || !ready) return;
    const fresh = now.filter((id) => !before.has(id));
    if (!fresh.length) return;
    setArriving((was) => new Set([...was, ...fresh]));
    const timer = setTimeout(() => {
      timers.current.delete(timer);
      setArriving((was) => {
        const next = new Set(was);
        for (const id of fresh) next.delete(id);
        return next.size ? next : NONE;
      });
    }, ARRIVE_FLASH_MS);
    timers.current.add(timer);
  }, [ids, ready]);
  useEffect(() => {
    const pending = timers.current;
    return () => {
      for (const timer of pending) clearTimeout(timer);
      pending.clear();
    };
  }, []);
  return arriving;
}

export interface QueuedStripProps {
  lines: readonly QueuedLine[];
  /** A turn is running: a line can be sent now (Ctrl+X), interrupting it. */
  running: boolean;
  onSendNow: () => void;
  /** Put an outbox line back into the box, keyed by line id. */
  onPull: (id: string) => void;
  /** A card tile: read-only. The lines are listed, with no send-now or edit. */
  compact?: boolean;
  /** The chat has read its history and is not adopting a run: from here on a
   *  new line is an arrival. Until then the lines are the ones that were
   *  already waiting (`useArrivals`). Absent means settled. */
  ready?: boolean;
}

export function QueuedStrip({ lines, running, onSendNow, onPull, compact, ready = true }: QueuedStripProps) {
  const arriving = useArrivals(lines, ready);
  if (!lines.length) return null;
  return (
    <ul className={compact ? "c-queue-strip c-queue-compact" : "c-queue-strip"} aria-label="Queued messages">
      {lines.map((line) => (
        <li className={arriving.has(line.id) ? "c-queue-row is-arriving" : "c-queue-row"} key={line.id}>
          <span className="c-queue-text">{line.text}</span>
          {compact || line.pending === "sending" ? null : line.pending === "held" ? (
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
          {!compact && line.pending && line.pending !== "sending" ? (
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
