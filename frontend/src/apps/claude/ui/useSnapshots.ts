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
import {
  cacheSnapshots,
  cachedSnapshots,
  invalidateSnapshots,
  isFileTarget,
  loadSnapshots,
} from "../protocol/snapshots";
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
  /**
   * T:19078-19082 `snapInvalidate` — A RUN THAT ENDED MAY HAVE EDITED THE FILE,
   * so the chain this panel drew is stale. Any value that CHANGES re-reads;
   * `undefined` never does.
   *
   * A counter rather than a method because the panel is unmounted while a chat
   * is on screen (the landing page owns it), so there is nothing to call — what
   * has to survive the round trip is the FACT that a run ended, and a number
   * bumped in the chat's own state is that fact. A host that keeps the panel
   * mounted gets a live re-read out of the same knob.
   */
  invalidation?: unknown,
  /**
   * The two calls, injectable — and injectable rather than module-mocked for the
   * reason `useSchedule`'s three are: `bun test` runs every suite in ONE
   * process, so a `mock.module("../protocol/snapshots", …)` replaces that module
   * for every suite loaded AFTER it (and an ESM namespace object is frozen
   * besides, so patch-and-restore is not open either).
   */
  deps?: {
    load?: typeof loadSnapshots;
    isFile?: typeof isFileTarget;
  },
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
  /** Read by `adopt`, which is a callback and must not be rebuilt for a new
   *  invalidation value (the rows close over it). */
  const invRef = useRef(invalidation);
  invRef.current = invalidation;
  /** Read at CALL time so a caller passing a fresh object each render cannot
   *  re-run the read (the effect's deps are the target and the two nonces). */
  const hooks = useRef({
    load: deps?.load ?? loadSnapshots,
    isFile: deps?.isFile ?? isFileTarget,
  });
  hooks.current = {
    load: deps?.load ?? loadSnapshots,
    isFile: deps?.isFile ?? isFileTarget,
  };
  const fileRef = useRef(file);
  fileRef.current = file;

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
      if (!(await hooks.current.isFile(file))) {
        if (live && gen.current === mine) setTimeline(undefined);
        return;
      }
      if (!live || gen.current !== mine) return;
      // ALREADY READ ONCE ON THIS PAGE: repaint what we have rather than spend
      // the round trip again (T:19105-19107). This is the whole of P4-22 — the
      // hook is inside `Home`, which unmounts on the way into a chat, so
      // without a page-scoped cache every Back re-read the timeline.
      //
      // NOT a read-through-and-refresh: T does not re-read either, and a
      // background read landing under a reader who is mid-expansion would move
      // the rows out from under them. The two things that DO make it stale —
      // a finished turn and a write — both go through the cache's own key or
      // through `reload`.
      const hit = cachedSnapshots(file, invalidation);
      if (hit) {
        setTimeline(hit);
        setFailed(false);
        setError("");
        return;
      }
      // Mounted and reading: the panel shows standalone so the note has
      // somewhere to be, and earns no tab yet.
      setTimeline(null);
      setFailed(false);
      setError("");
      try {
        const out = await hooks.current.load(agentDir, file);
        if (!live || gen.current !== mine) return;
        // Cached even when a newer generation is about to replace it? No — the
        // guard above already returned. A FAILED read caches nothing, so the
        // retry and the next landing ask again rather than leaving the section
        // stuck on the failure for the life of the page (T:19044-19047).
        cacheSnapshots(file, invalidation, out);
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
  }, [agentDir, file, nonce, invalidation]);

  /** The heading's retry, and the way a revert repaints when it has no timeline
   *  of its own to hand back. Drops the cached entry FIRST: a retry that read
   *  the cache back would be a control that does nothing. */
  const reload = useCallback(() => {
    if (fileRef.current) invalidateSnapshots(fileRef.current);
    setNonce((n) => n + 1);
  }, []);
  const adopt = useCallback((next: SnapshotsTimeline) => {
    gen.current += 1;
    // A WRITE'S OWN ANSWER IS THE FRESHEST THERE IS (T:19042-19043 — "snapGoBack
    // repaints from the post-revert timeline the write itself returned"), so it
    // becomes the cache rather than invalidating it: the next landing repaints
    // the post-revert chain without a round trip.
    if (fileRef.current) cacheSnapshots(fileRef.current, invRef.current, next);
    setTimeline(next);
    setFailed(false);
    setError("");
  }, []);

  return { timeline, failed, error, reload, adopt };
}
