// The pages Claude published DURING THIS CHAT, as a chip strip above the
// footnote (T:4203, T:18570-18632; inventory 05 §F).
//
// ACCUMULATING, keyed by url: the poll reads the transcript of whichever session
// is current, and a chip must not vanish because a turn ended or a later read
// raced. Cleared only by leaving for the landing page — a different
// conversation's pages are not this one's — or by a reload.
//
// The strip is DECORATION driven off the reply's own clock: `onArtifactsTick`
// fires every 8th poll (~3.2 s) and once at the run's end, which is where T
// hangs it (T:16229, 16330). A read that fails is a console line, never an
// interruption of the poll for the reply.
import { useCallback, useEffect, useRef, useState } from "react";
import "../styles/sched.css";
import {
  artLabel,
  artSignature,
  pollArtifacts,
  type Artifact,
} from "../protocol/artifacts";

export interface ArtStripProps {
  items: readonly Artifact[];
}

export function ArtStrip({ items }: ArtStripProps) {
  if (!items.length) return null;
  return (
    <div className="c-artstrip" aria-label="Pages Claude published in this chat">
      {items.map((a) => (
        <a
          key={a.remote_url}
          className="art-chip"
          href={a.remote_url}
          target="_blank"
          rel="noopener noreferrer"
          title={"Open " + artLabel(a)}
        >
          {/* `◻` is the fallback, and it is TEXT: a favicon comes off the wire
              as an emoji or a single glyph, never as markup. */}
          <span className="art-ic">{a.favicon || "◻"}</span>
          <span className="art-chip-lbl">{artLabel(a)}</span>
          <span className="art-chip-go" aria-hidden="true">
            ↗
          </span>
        </a>
      ))}
    </div>
  );
}

export interface ArtStripStore {
  items: Artifact[];
  /** One transcript read. Safe to call from the run loop's tick. */
  poll(): void;
  /** Leaving for the landing page (T:13093 `clearArtStrip`). */
  clear(): void;
}

/**
 * The Map behind the strip, and the change detection that keeps it still.
 *
 * BY CONTENT, NOT BY COUNT (T:18627-18634): the favicon/title join can land a
 * poll or two after the frame-link, so an already-seen url can gain metadata
 * without the set growing. And still no re-render when nothing changed — this
 * runs every few seconds beside a streaming reply, and rebuilding an unchanged
 * strip would drop the user's mid-click on a chip.
 */
export function useArtStrip(
  agentDir: string | null,
  file: string | null,
  sessionId: string,
  /** The transcript read, injectable so a suite drives the strip without
   *  replacing a module for every suite that loads after it. */
  read: typeof pollArtifacts = pollArtifacts,
): ArtStripStore {
  const chips = useRef(new Map<string, Artifact>());
  const [items, setItems] = useState<Artifact[]>([]);
  const busy = useRef(false);
  // Read at POLL time, never captured: the tick comes from the run loop, which
  // outlives any one render of this hook.
  const live = useRef({ agentDir, file, sessionId });
  live.current = { agentDir, file, sessionId };

  const readRef = useRef(read);
  readRef.current = read;
  /**
   * A TICK THAT ARRIVED TOO EARLY, KEPT (Bugbot PR #1075).
   *
   * On a brand-new chat the run loop notes the session id and fires
   * `onArtifactsTick` inside the SAME poll, so the first tick — and the end
   * tick of a short first turn — reach this hook before React has re-rendered
   * it with the id. Dropping those was a strip that stayed empty until a
   * reload, however many pages the turn published. So the tick is remembered
   * and replayed the moment the id lands, which is the same shape `useTaskId`
   * and `useSnapshots` take: the session id is the effect's key, not something
   * a callback closes over.
   */
  const owed = useRef(false);
  const poll = useCallback(() => {
    const { agentDir: dir, file: target, sessionId: sid } = live.current;
    if (!dir || !sid) {
      // Not a read and not a retry loop: one flag, spent by the next render
      // that has an id. A chat that never gets one never reads.
      owed.current = true;
      return;
    }
    if (busy.current) return;
    busy.current = true;
    void (async () => {
      try {
        const rows = await readRef.current(dir, target, sid);
        // The await straddles navigation: Back may have cleared the strip (and
        // the session) while this read was in flight, and a fresh chat may even
        // be underway. A stale answer must evaporate, not repopulate the strip.
        if (live.current.sessionId !== sid) return;
        if (!rows.length) return;
        const before = artSignature(chips.current.values());
        // A ROW WITH NO URL IS NOT A CHIP. `remote_url` is the Map's key and the
        // anchor's href both, so an unpublished artifact would take the
        // `undefined` slot — one entry no matter how many arrive, a duplicate
        // React key, and a chip that goes nowhere when pressed. There is
        // nothing to open, so there is nothing to draw.
        for (const a of rows) if (a.remote_url) chips.current.set(a.remote_url, a);
        if (artSignature(chips.current.values()) !== before) {
          setItems(Array.from(chips.current.values()));
        }
      } finally {
        busy.current = false;
      }
    })();
  }, []);

  // The re-arm. Keyed on the two facts a read needs, so it fires on the render
  // that brings either of them and on no other.
  useEffect(() => {
    if (!owed.current || !agentDir || !sessionId) return;
    owed.current = false;
    poll();
  }, [agentDir, sessionId, poll]);

  const clear = useCallback(() => {
    if (!chips.current.size) return;
    chips.current.clear();
    setItems([]);
  }, []);

  return { items, poll, clear };
}
