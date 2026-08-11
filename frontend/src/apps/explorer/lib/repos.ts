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
 * `liveScanning` is the index poll's `scanning`, or null when nothing has polled
 * yet. It wins over the response's own (older) copy whenever it exists.
 */
export function reposView(
  response: GitRepos | null,
  failed: boolean,
  liveScanning: boolean | null,
): ReposView {
  if (failed) return { kind: "failed" };
  if (response === null) return { kind: "loading" };
  const scanning = liveScanning ?? response.scanning;
  // `indexed` is the only bit that decides whether there is an ANSWER, and a
  // scan in flight never downgrades one: a rescan keeps serving the last completed
  // generation (index-store.md §4), and the whole point of `stale` is that a list
  // a generation behind beats no list. It only becomes a note on the cards.
  if (response.indexed) {
    return { kind: "ready", repos: response.repos, stale: response.stale || scanning };
  }
  // No answer available. `outdated` is its own state because the remedy differs:
  // nothing has to be done, a rebuild is already forced — where `unavailable`
  // genuinely needs the user to start one.
  if (scanning) return { kind: "building" };
  return response.reason === "outdated" ? { kind: "outdated" } : { kind: "unavailable" };
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
