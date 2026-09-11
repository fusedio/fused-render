// THE LANDING'S THREE READS, IN ONE ORDER (T:19282-19293, P4-14).
//
// T sequences them explicitly, and every step of the sequence is argued in the
// template:
//
//     focusBox(homebox);
//     await loadRecent();       // the session list, first
//     markChatReady();          // ...and the host may uncover us only now
//     watchRecent();
//     await mountSnapshots();   // then the local checkpoint chain
//     loadArtifacts();          // last, and unawaited
//
// The two claims that make it an ORDER rather than three independent effects:
//
//   * `markChatReady()` comes AFTER the session list. The host uncovers the
//     pane on that signal, so firing it first shows a landing whose one list is
//     still a skeleton — the state the read is about to replace.
//   * `loadArtifacts()` is "last of the three landing reads, and unawaited by
//     nothing above it on purpose: it is the only one that leaves the machine (a
//     localhost call to the artifacts index), so it must not sit in front of the
//     session list" (T:19290-19293).
//
// Native fired all three as independent effects on one commit and called ready
// immediately, so both claims were lost: the host could uncover the pane before
// any list had rows, and the artifacts index call could be in flight ahead of
// `sessions`.
//
// The session list itself is NOT owned here. It is subscribed above the landing
// (`ClaudeChat`'s `useRecentSessions`) because the chat's Back path is what
// knows whether the two write-covering re-reads are owed (P4-21) and because the
// rows must survive the `Home` unmount (P4-08b). So this takes the list's answer
// as its FIRST STEP rather than making it, which is also what lets the two reads
// below it be ordered without the list being re-read on every landing.
import type { Artifact, loadArtifacts } from "../protocol/artifacts";
import type { isFileTarget, loadSnapshots } from "../protocol/snapshots";
import { useArtifacts } from "./useArtifacts";
import { useSnapshots, type SnapshotsState } from "./useSnapshots";

export interface LandingReads {
  artifacts: Artifact[] | null;
  snaps: SnapshotsState;
  /** Has the session list answered? The `markReady` gate, published so the one
   *  caller that owns the ready signal does not have to re-derive it. */
  sessionsIn: boolean;
}

export function useLandingReads(
  agentDir: string | null,
  file: string | null,
  /** The session list as `useRecentSessions` publishes it: `null` until the
   *  first read answers. */
  recent: unknown[] | null,
  /** T:19078 `snapInvalidate` — see `useSnapshots`. */
  snapInvalidation?: unknown,
  /** The two reads, injectable through the owner so a suite can observe the
   *  ORDER rather than either hook in isolation. Same reason every other seam
   *  in this app exists: `bun test` runs every suite in one process. */
  deps?: {
    snaps?: { load?: typeof loadSnapshots; isFile?: typeof isFileTarget };
    artifacts?: typeof loadArtifacts;
  },
): LandingReads {
  // STEP ONE, made by the caller: `await loadRecent()`.
  const sessionsIn = recent !== null;
  // STEP TWO: `await mountSnapshots()`, held behind the list.
  const snaps = useSnapshots(
    agentDir,
    file,
    snapInvalidation,
    deps?.snaps,
    sessionsIn,
  );
  // STEP THREE: `loadArtifacts()`, held behind BOTH — the only call that leaves
  // the machine goes last. `snaps.settled` is true for a failure and for a
  // target with no panel as well as for an answer, so nothing here can be
  // stranded behind a read that is never going to happen.
  const artifacts = useArtifacts(
    agentDir,
    file,
    deps?.artifacts,
    sessionsIn && snaps.settled,
  );
  return { artifacts, snaps, sessionsIn };
}
