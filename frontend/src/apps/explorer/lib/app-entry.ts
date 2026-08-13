// Which page a folder IS — the shell's copy of the one entry rule (D269).
//
// A folder with a top-level `.html` is an app, and its entry page is what every
// surface showing that folder should show: `index.html` when the folder has
// one, else the FIRST non-hidden top-level `.html` in NAME order. A folder with
// no top-level html has no entry, and is shown as the plain listing it is.
//
// This is `fused_render/templates/shared/app_entry.py::entry_html`, in
// TypeScript, and it must stay that file's twin to the letter: the preview pane
// resolves a folder here, the `claude` template resolves the same folder there,
// and the two showing different pages for one folder is the bug the shared
// module was written to prevent. It is duplicated rather than fetched because
// the pane already has the folder's listing in hand (one `/api/fs/list`, cached
// by prefetchListDir) and no endpoint answers this question on its own.
//
// Two divergences, both deliberate, both from the same "answer the question you
// were asked" split:
//
//   * `.htm` does NOT count — `.html` only, exactly as the Python rule reads,
//     because these two must agree on the same folder. `listing/selection.ts`'s
//     `isPageRow` DOES accept `.htm`, and that is not drift: it picks which row
//     the pane opens on inside a folder, where the registry renders `.htm` and
//     `.html` identically and being wrong costs one keystroke (D263, SPEC FS-16).
//   * `preview.png` is irrelevant here. `lib/folder-peek.ts` ranks it FIRST
//     because it decides what a folder card shows a PICTURE of; this decides
//     which page the folder opens, and a screenshot is not a page.
//
// (Its ancestor `lib/folder-app.ts` — same file name, narrower rule: it asked
// whether a folder had EXACTLY ONE top-level html, mirroring the server's
// `app_listing.app_entry` — was deleted by D264 with the app concept and is not
// what came back. D269 restored the DESTINATION, on the broad rule above, and
// widened the server copy to match it rather than reviving the narrow one.)
import type { FsEntry } from "@platform/lib/api";
import { join } from "@apps/explorer/lib/fs-actions";

function isEntryHtml(e: FsEntry): boolean {
  return !e.is_dir && !e.name.startsWith(".") && e.name.toLowerCase().endsWith(".html");
}

// The entry page's NAME among a folder's direct children, or null when the
// folder has none. Takes the entries rather than a path so the rule stays pure
// and the caller owns the fetch.
//
// Sorted here rather than trusted from the caller: "the first in name order" is
// only a fact if this module makes it one — /api/fs/list is sorted today, but a
// rule two languages have to agree on cannot rest on that.
export function entryHtmlName(entries: readonly FsEntry[]): string | null {
  const htmls = entries.filter(isEntryHtml).map((e) => e.name);
  if (htmls.length === 0) return null;
  const index = htmls.find((n) => n.toLowerCase() === "index.html");
  if (index) return index;
  return [...htmls].sort()[0];
}

// The same answer as an absolute path under `dir`, for a caller that wants to
// navigate to or preview the page.
export function entryHtmlPath(dir: string, entries: readonly FsEntry[]): string | null {
  const name = entryHtmlName(entries);
  return name === null ? null : join(dir, name);
}

// Where a card FOR a folder goes when it is clicked, and what its href carries.
// The same shape of answer `platform/lib/appEntry.ts::openTargetFor` gives an
// /apps hub card, for the surfaces that hold a folder's LISTING rather than the
// server's resolved `entry`: the homepage's Repos and Artifacts cards.
//
// `entries === null` means "no listing in hand", and it is deliberately the
// same answer as "no page in there": the folder itself. It covers the card's
// first render, before its `/api/fs/list` lands, and its last one if that list
// never lands at all — an unreadable folder, a dead mount. So the card is
// clickable from the first frame, never shows a spinner in place of a
// destination, and degrades to exactly the folder navigation it had before this
// rule reached the surface.
//
// That null case is also what keeps the ANCHOR honest. Both the href and the
// click handler read one of these values per render, so they cannot disagree;
// what the href may do is LAG — carry the folder until the listing resolves,
// then upgrade to the page — which is a link that is late, not a link that is
// wrong. (Middle-clicking a card in the frame before its listing lands opens
// the folder listing; the page is one crumb-click away, and the alternative —
// an anchor with no href, or one pointing at a page nothing has confirmed
// exists — is worse in both directions.)
//
// `isDir` rides into `navigate` as the nav hint (router's navHintIsDir), so the
// destination paints the right scaffold before its own stat resolves. A caller
// holding a listing KNOWS which kind it resolved, so it is always stated.
export interface FolderOpenTarget {
  path: string;
  isDir: boolean;
}

export function folderOpenTarget(
  dir: string,
  entries: readonly FsEntry[] | null,
): FolderOpenTarget {
  const entry = entries === null ? null : entryHtmlPath(dir, entries);
  return entry === null ? { path: dir, isDir: true } : { path: entry, isDir: false };
}
