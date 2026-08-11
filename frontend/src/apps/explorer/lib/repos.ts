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
  // The index cannot answer yet AND a scan is in flight — it is coming.
  | { kind: "building" }
  // The index cannot answer and nothing is scanning: it was never built, or a
  // scan ended without producing one (cancelled, failed). Needs the user.
  | { kind: "unavailable" }
  // The index answered. `repos` may be empty, and that is a real answer.
  | { kind: "ready"; repos: GitRepo[] };

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
  // `indexed` is the only bit that decides whether there is an ANSWER. A rescan
  // over a usable index keeps serving the last completed generation
  // (index-store.md §4), so scanning never downgrades a real answer to "wait".
  if (response.indexed) return { kind: "ready", repos: response.repos };
  return (liveScanning ?? response.scanning)
    ? { kind: "building" }
    : { kind: "unavailable" };
}

export function reposMessage(view: ReposView): string {
  switch (view.kind) {
    case "loading":
      return "Looking for repos…";
    case "failed":
      return "Couldn't read the list of repos.";
    case "building":
      return "Still building the file index — repos will appear when the first scan finishes.";
    case "unavailable":
      // Deliberately actionable: this is the state where nothing is going to
      // happen on its own, including after a scan was cancelled or failed.
      return "The file index hasn't been built yet, so there's nothing to list repos from. Rebuild it from Preferences → Indexing.";
    case "ready":
      return "No git repositories found on this machine.";
  }
}
