// The Repos tab's empty-state copy (FilesHome.tsx).
//
// The repo list is DERIVED from the file index, so an empty list is ambiguous in
// a way the other tabs' empty lists are not: bookmarks and recents are local
// state that is simply empty, but "no repos" can mean the first index scan
// hasn't finished. Saying "no repos found" then is a lie about the machine, and
// the user has no way to tell it apart from the truth — so the three cases get
// three messages. Pure, so it can be tested without rendering the page.
import type { GitRepos } from "@platform/lib/api";

export function emptyReposMessage(r: GitRepos): string {
  if (r.indexed) return "No git repositories found on this machine.";
  // Not indexed. `scanning` separates "wait, it's happening" from "nothing has
  // ever indexed this machine", which needs the user to go start a scan.
  return r.scanning
    ? "Still building the file index — repos will appear when the first scan finishes."
    : "The file index hasn't been built yet, so there's nothing to list repos from. Rebuild it from Preferences → Indexing.";
}
