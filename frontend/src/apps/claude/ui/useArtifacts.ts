// The landing page's Artifacts list, read once per mount of the landing view
// (T:18547 `loadArtifacts`, called on every path ONTO the landing page).
//
// `null` is "the read has not answered" and `[]` is "nothing was published from
// here", which is the common case and the reason the section can be absent
// altogether: no heading, no tab, no empty state, no row saying so.
import { useEffect, useState } from "react";
import { loadArtifacts, type Artifact } from "../protocol/artifacts";

/**
 * THE LAST ANSWER FOR EACH TARGET, kept at page scope (P4-08 / C G-20b).
 *
 * T re-reads on every path onto the landing but deliberately does NOT reset the
 * count first: "both reads are about to run again and land within a few hundred
 * ms, and clearing them first would take the tab bar off screen and put it back
 * for the trip — a stale count for a moment is quieter than a section that
 * blinks. Boot is the only place the 'not read yet' state is real."
 * (T:13049-13053.)
 *
 * Natively the count lives in this hook, inside `Home`, which unmounts on the
 * way into a chat — so `null` came back on every Back and the tab bar blinked.
 * The rows are remembered here instead and seeded on mount, and the read still
 * runs: unlike the snapshots timeline (which T caches outright), the artifacts
 * INDEX is the one read that leaves the machine, and a published page is exactly
 * the thing a turn just changed.
 */
const lastRows = new Map<string, Artifact[]>();

const key = (agentDir: string, file: string | null) => agentDir + "\u0000" + (file ?? "");

/** Test seam: page scope means one suite's rows are visible to the next. */
export function resetArtifactsMemoryForTests(): void {
  lastRows.clear();
}

export function useArtifacts(
  agentDir: string | null,
  file: string | null,
  /** The read, injectable for the reason every other seam in this app is: one
   *  process, every suite. */
  read: typeof loadArtifacts = loadArtifacts,
  /**
   * LAST OF THE THREE LANDING READS, and T says why it is last: "it is the only
   * one that leaves the machine (a localhost call to the artifacts index), so
   * it must not sit in front of the session list" (T:19290-19293). False holds
   * it; `undefined`/true is "go now".
   */
  enabled = true,
): Artifact[] | null {
  const [rows, setRows] = useState<Artifact[] | null>(() =>
    agentDir ? (lastRows.get(key(agentDir, file)) ?? null) : null,
  );
  useEffect(() => {
    if (!agentDir) {
      setRows(null);
      return;
    }
    const seat = key(agentDir, file);
    if (!enabled) {
      // Still seeded from the memory, so the tab bar's count is whatever it
      // last honestly was while the two reads ahead of this one run.
      setRows(lastRows.get(seat) ?? null);
      return;
    }
    let live = true;
    // The previous answer for THIS target stands while the fresh one is in
    // flight; a target we have never read is the only `null` (see above).
    setRows(lastRows.get(seat) ?? null);
    void read(agentDir, file)
      .then((out) => {
        lastRows.set(seat, out);
        if (live) setRows(out);
      })
      // AND IT FAILS OPEN, QUIETLY (batch review F5). This is the one read that
      // leaves the machine, so a rejection is ordinary — and without a `.catch`
      // it was an unhandled promise rejection, next to `useSnapshots` and
      // `subscribeRecent` which both answer for their own failures. Nothing is
      // published, so the remembered rows and the tab bar's count stay at
      // whatever they last honestly were; the next landing asks again. T fails
      // open here too — its `pollArtifacts` has no error branch at all.
      .catch((err: unknown) => {
        console.warn("artifacts failed:", err instanceof Error ? err.message : err);
      });
    return () => {
      live = false;
    };
  }, [agentDir, file, enabled]);
  return rows;
}
