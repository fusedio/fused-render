// What the Explorer homepage's Repos tab is currently showing, and the copy for
// each state.
//
// This exists as ONE total function over the inputs because deriving the state ad
// hoc did not work. The tab has two independently-updating sources — a
// /api/git-repos response (which can be seconds old) and the live index poll — and
// three rounds of bugs all came from the same shape: reading a boolean from one
// source next to a boolean from the other produces intermediate combinations that
// are not real states. "Still building" forever (no refetch), then the opposite
// flicker ("go rebuild the index" for one frame right after a scan finished), then
// "Still building" forever again for a CANCELLED scan. Each patch created the next
// edge case.
//
// So the states are enumerated instead of derived, the impossible combinations are
// not representable, and the freshness problem is solved where it belongs: the
// caller refetches whenever the index's observable state changes AT ALL (see
// FilesHome), so a scan that completes, is cancelled, fails, or is killed all end
// with a fresh response. That is what lets `liveScanning` simply win here rather
// than being clamped one way — the clamp was the previous fix, and it was a clamp
// precisely because the refetch could not be trusted to happen.
import type { GitRepo, GitRepos } from "@platform/lib/api";

export type ReposView =
  // No response yet. Distinct from "ready with nothing", which is an answer.
  | { kind: "loading" }
  // The request itself failed; the index's state is unknown.
  | { kind: "failed" }
  // Nothing to show AND a scan is in flight — a list is coming.
  | { kind: "building" }
  // Nothing to show, nothing running: no index was ever built, or a scan ended
  // without producing one (cancelled, failed). Needs the user.
  | { kind: "unavailable" }
  // The index predates repo detection, so its zero rows are not an answer. A
  // rebuild is forced on the next scan; nothing for the user to fix.
  | { kind: "outdated" }
  // The index answered. `repos` may be empty, and that is a real answer.
  // `stale` folds INTO this variant rather than sitting beside the union, so
  // "showing a list" and "the list might be a little behind" cannot drift apart:
  // a stale ready state still renders every card, just with a quiet note.
  | { kind: "ready"; repos: GitRepo[]; stale: boolean };

/**
 * Everything the view depends on. A named object rather than positional booleans
 * on purpose: "two independently-updating booleans read next to each other" is the
 * literal shape of every bug this file has had, and at three of them a positional
 * signature is a trap.
 */
export interface ReposInputs {
  /** Last SUCCESSFUL /api/git-repos body, or null before the first one. */
  response: GitRepos | null;
  /** The most recent fetch rejected. */
  failed: boolean;
  /** The index poll's `scanning`, or null when nothing has polled yet. */
  liveScanning: boolean | null;
  /**
   * A /api/git-repos request is in flight whose result has not been applied yet.
   *
   * This is the input whose absence caused the scan-end flicker twice. "A refetch
   * is triggered" is not the same claim as "the new answer has arrived", and the
   * whole gap between those two is a window where the response on hand describes a
   * world that no longer exists. It must be derived during render (FilesHome
   * compares the key the response was fetched under against the current one), not
   * set from an effect — an effect lands one frame late, which is exactly the
   * flicker in miniature.
   */
  refreshPending: boolean;
}

/**
 * The complete state table. Rows are ordered by the priority the guards apply,
 * every cell is reachable, and the two invariants that killed five bugs are:
 *
 *   NEVER REGRESS  — a state on screen is never replaced by a worse one just
 *                    because a transient input flipped. A held list survives a
 *                    failed refresh and an in-flight rescan.
 *   NEVER ACTIONABLE WHILE SOMETHING IS COMING — the "go rebuild it" copy is
 *                    unreachable while a scan runs OR a refresh is pending, so it
 *                    can only appear when nothing is going to fix itself.
 *
 * | response | failed | scanning | pending | reason   | -> view            |
 * |----------|--------|----------|---------|----------|--------------------|
 * | null     | no     | any      | any     | -        | loading            |
 * | null     | YES    | any      | any     | -        | failed             |
 * | indexed  | any    | no       | any     | -        | ready (stale?)     |
 * | indexed  | any    | YES      | any     | -        | ready + stale      |
 * | no list  | any    | YES      | any     | any      | building           |
 * | no list  | any    | no       | YES     | any      | building  <- #7    |
 * | no list  | any    | no       | no      | outdated | outdated           |
 * | no list  | any    | no       | no      | other    | unavailable        |
 *
 * `scanning` is `liveScanning ?? response.scanning`: the live poll wins when we
 * have one, the response's own copy bridges the first render.
 */
export function reposView({
  response,
  failed,
  liveScanning,
  refreshPending,
}: ReposInputs): ReposView {
  // `failed` only decides the outcome when there is nothing else to show. Throwing
  // away 21 good cards because one refresh timed out would be the never-regress
  // rule violated at its most visible.
  if (response === null) return failed ? { kind: "failed" } : { kind: "loading" };
  const scanning = liveScanning ?? response.scanning;
  // `indexed` is the only bit that decides whether there is an ANSWER, and nothing
  // downgrades one: a rescan keeps serving the last completed generation
  // (index-store.md §4), and the point of `stale` is that a list a generation
  // behind beats no list. Both only add a note to the cards.
  if (response.indexed) {
    return { kind: "ready", repos: response.repos, stale: response.stale || scanning };
  }
  // No answer to show. Anything in motion means one is coming, so the holding
  // message is used and the actionable ones are out of reach.
  if (scanning || refreshPending) return { kind: "building" };
  // Nothing in motion. `outdated` is still distinct from `unavailable` because its
  // remedy differs: a rebuild is already forced by the rules change, so telling the
  // user to go start one would be busywork.
  return response.reason === "outdated" ? { kind: "outdated" } : { kind: "unavailable" };
}

/**
 * Whether the tab should keep polling the index. True until the answer is FINAL —
 * indexed and not stale. Gating on `!indexed` (the previous rule) stopped the poll
 * the moment a stale-but-served list arrived, which froze the refetch key and left
 * the cards and their "Reindexing" note on screen permanently.
 *
 * The cost, named: a `stale` that nothing will clear on its own — a configured root
 * the user never rescans — keeps the poll on its 10s idle heartbeat for as long as
 * the homepage is open. That is the same idle heartbeat the listing's search box
 * pays, and it is the price of never being stuck.
 */
export function reposNeedsIndexPoll(response: GitRepos | null): boolean {
  return !(response !== null && response.indexed && !response.stale);
}

/** The quiet note shown above a stale list, or null when there is nothing to say. */
export function reposStaleNote(view: ReposView): string | null {
  return view.kind === "ready" && view.stale
    ? "Reindexing — this list may be out of date."
    : null;
}

export function reposMessage(view: ReposView): string {
  switch (view.kind) {
    case "loading":
      return "Looking for repos…";
    case "failed":
      return "Couldn't read the list of repos.";
    case "building":
      return "Still building the file index — repos will appear when the first scan finishes.";
    case "outdated":
      // Not actionable on purpose: the rules change already forces a rescan, so
      // sending the user to Preferences would be busywork.
      return "The file index predates repo detection — repos will appear after the next scan.";
    case "unavailable":
      // Deliberately actionable: this is the state where nothing is going to
      // happen on its own, including after a scan was cancelled or failed.
      return "The file index hasn't been built yet, so there's nothing to list repos from. Rebuild it from Preferences → Indexing.";
    case "ready":
      return "No git repositories found on this machine.";
  }
}
