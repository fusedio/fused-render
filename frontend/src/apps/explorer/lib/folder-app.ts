// Is this folder an app, and which page is its entry — the shell's copy of the
// server's rule (fused_render/app_listing.py::app_entry).
//
// It is a COPY on purpose, and the duplication is the lesser evil: the page has
// a listing in hand and asking the server a second question about a folder it
// just listed is a round trip for an answer already on screen. What is not
// negotiable is that the two agree, because they gate two halves of one
// journey — `app_entry` decides whether the /apps hub calls the folder an app,
// and this decides whether the explorer offers "Open as app" once you are
// standing in it. When they disagreed, a folder was an app you could not open:
// a directory has no top-bar mode switcher (that is the explorer's deliberate
// one-switcher rule, Preview.tsx), so the button is the only door and a
// withheld button is a dead end.
//
// Deliberately NOT `isAppEntry` (platform/ui/FileIcons), which the listing uses
// to badge rows and to say "Open App" in the row menu. That one answers "can
// this FILE be launched as a page", where `.htm` is a page and a dotfile is
// still openable — both right for a row, both wrong here. Sharing it was the
// bug: `index.html` beside a `legacy.htm` counted as two entries and withheld
// the button from a folder the hub had already called an app.
import type { FsEntry } from "@platform/lib/api";

// One entry, matching app_entry's filter exactly: a non-hidden `.html` FILE.
// The extension test is case-insensitive because the server's is (`.lower()`);
// the leading-dot and directory tests are the other two clauses of it.
function isEntryPage(e: FsEntry): boolean {
  return !e.is_dir && !e.name.startsWith(".") && e.name.toLowerCase().endsWith(".html");
}

// The folder's lone entry page by name, or null when it has zero or several —
// which is app_entry's `len(htmls) != 1` and means "not an app of this shape",
// not "unreadable". Callers turn the name into a path against their own base.
//
// Names, not paths, so the caller owns the join (the two call sites have
// different bases in hand — one the listing's, one the previewed row's).
export function loneEntryPage(entries: FsEntry[]): string | null {
  const pages = entries.filter(isEntryPage);
  return pages.length === 1 ? pages[0].name : null;
}
