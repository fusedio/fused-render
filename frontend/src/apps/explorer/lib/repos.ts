// The Repos tab's empty-state copy (FilesHome.tsx).
//
// The repo list is DERIVED from the file index, so an empty list is ambiguous in
// a way the other tabs' empty lists are not: bookmarks and recents are local
// state that is simply empty, but "no repos" can mean the first index scan
// hasn't finished. Saying "no repos found" then is a lie about the machine, and
// the user has no way to tell it apart from the truth — so the three cases get
// three messages. Pure, so it can be tested without rendering the page.
import type { GitRepos } from "@platform/lib/api";

// Fold the live index poll's `scanning` into a (possibly stale) /api/git-repos
// response. The poll may only RAISE the flag, never lower it.
//
// Raising is the useful half: the empty state moves from "no index — go rebuild
// it" to "still building" the moment a scan starts, without waiting for a refetch.
// Lowering would be actively wrong — the instant a scan finishes `scanning` goes
// false while `indexed` is still false from the pre-scan response, so the tab would
// render "go rebuild the index from Preferences" for as long as the refetch takes,
// telling the user to do something that just happened. The refetch is already in
// flight (FilesHome keys it on that same completion), so holding the previous
// message until the real answer lands is both correct and shorter. Once it lands,
// `scanning` comes fresh from the server and this is a no-op.
export function withLiveScanning(r: GitRepos, liveScanning: boolean | null): GitRepos {
  if (liveScanning === null) return r;
  return { ...r, scanning: r.scanning || liveScanning };
}

export function emptyReposMessage(r: GitRepos): string {
  if (r.indexed) return "No git repositories found on this machine.";
  // Not indexed. `scanning` separates "wait, it's happening" from "nothing has
  // ever indexed this machine", which needs the user to go start a scan.
  return r.scanning
    ? "Still building the file index — repos will appear when the first scan finishes."
    : "The file index hasn't been built yet, so there's nothing to list repos from. Rebuild it from Preferences → Indexing.";
}
