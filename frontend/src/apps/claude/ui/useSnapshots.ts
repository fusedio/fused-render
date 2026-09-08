// The landing page's snapshots timeline (T:19086-19175 `mountSnapshots` /
// `loadSnapshots`).
//
// THREE STATES and they are not interchangeable. `null` = mounted and reading,
// which is what gives the "reading the version history…" note somewhere to be
// without yet earning a tab (a tab that appears and then withdraws is worse
// than a late one). A timeline = the answer. `failed` = the one absence that
// KEEPS its place in the block, because the retry beside the label is the only
// way back from it.
//
// NOT MOUNTED AT ALL for a folder: the store keys on one absolute file path, so
// a folder has no chain and `agent.py` refuses one. The gate is `isFileTarget`
// (the shell's own cached stat) rather than a prop, so the panel can never
// disagree with the rest of the page and nothing new has to be threaded through
// the chat.
import { useCallback, useEffect, useRef, useState } from "react";
import { isFileTarget, loadSnapshots } from "../protocol/snapshots";
import type { SnapshotsTimeline } from "../protocol/types";

export interface SnapshotsState {
  /** `undefined` = this target has no panel (a folder, or not decided yet);
   *  `null` = mounted and reading; a timeline = the answer. */
  timeline: SnapshotsTimeline | null | undefined;
  /** The read failed. The panel stays, holding the retry (T:19168-19174). */
  failed: boolean;
  /** The reason, verbatim, for the note under the rows. */
  error: string;
  /** The retry beside the heading, and the way a revert repaints. */
  reload(): void;
  /** A write handed back a post-revert timeline: repaint from it rather than
   *  spending a second round trip, for the length of which the list would go on
   *  showing the pre-revert position (T:19012-19017). */
  adopt(next: SnapshotsTimeline): void;
}

export function useSnapshots(
  agentDir: string | null,
  file: string | null,
): SnapshotsState {
  const [timeline, setTimeline] = useState<SnapshotsTimeline | null | undefined>(
    undefined,
  );
  const [failed, setFailed] = useState(false);
  const [error, setError] = useState("");
  const [nonce, setNonce] = useState(0);
  // A repaint from a write must not be undone by a read that was already in
  // flight when it landed.
  const gen = useRef(0);

  useEffect(() => {
    gen.current += 1;
    const mine = gen.current;
    if (!agentDir || !file) {
      setTimeline(undefined);
      setFailed(false);
      return;
    }
    let live = true;
    void (async () => {
      if (!(await isFileTarget(file))) {
        if (live && gen.current === mine) setTimeline(undefined);
        return;
      }
      if (!live || gen.current !== mine) return;
      // Mounted and reading: the panel shows standalone so the note has
      // somewhere to be, and earns no tab yet.
      setTimeline(null);
      setFailed(false);
      setError("");
      try {
        const out = await loadSnapshots(agentDir, file);
        if (!live || gen.current !== mine) return;
        setTimeline(out);
        setFailed(false);
      } catch (err) {
        if (!live || gen.current !== mine) return;
        // An ordinary absence, never the red overlay: a file Claude has never
        // touched is the common case, not a bug in this view.
        console.warn(
          "snapshots failed:",
          err instanceof Error ? err.message : String(err),
        );
        setTimeline(null);
        setFailed(true);
        setError(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => {
      live = false;
    };
  }, [agentDir, file, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  const adopt = useCallback((next: SnapshotsTimeline) => {
    gen.current += 1;
    setTimeline(next);
    setFailed(false);
    setError("");
  }, []);

  return { timeline, failed, error, reload, adopt };
}
