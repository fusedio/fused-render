// Shared types and tuning constants for the directory listing view.
// See Listing.tsx for the top-level architecture notes.
import type { FsEntry, WalkEntry } from "@platform/lib/api";

// A right-clicked row, normalized so both listing rows (name relative to the
// listed folder) and search-result rows (a `rel` path into a subtree) drive the
// same menu. `parentDir` is the containing folder; `path` is the entry itself.
export interface RowCtx {
  path: string;
  name: string;
  isDir: boolean;
  parentDir: string;
}

// One open modal: a text prompt (New File/Folder, Rename) or a confirm (Delete).
export type DialogState =
  | {
      kind: "prompt";
      title: string;
      initial: string;
      confirmLabel: string;
      selectStem?: boolean;
      onConfirm: (value: string) => void;
    }
  | {
      kind: "confirm";
      title: string;
      message: React.ReactNode;
      confirmLabel: string;
      danger?: boolean;
      onConfirm: () => void;
    };

export const SORT_KEYS = { name: "Name", size: "Size", mtime: "Modified" };
export type SortKey = keyof typeof SORT_KEYS;
export type SortOrder = "asc" | "desc";

// Columns the listing table renders. Search mode shows the matched PATH alone:
// a hit's row is already a full rel path, and it needs every pixel the table
// has. Status, banner and sentinel rows span the table, so their colSpan must
// follow the mode — a hardcoded 3 under a one-column head declares two columns
// nothing else mentions, and table-layout:fixed hands them width (the dead
// strip in listing/column-shedding).
export function columnCount(searching: boolean): number {
  return searching ? 1 : 3;
}

// Search-result rows rendered per "page". A ranked answer can carry up to
// SEARCH_RANK_LIMIT hits; mounting them all as <tr>s at once is what jams the
// main thread. Scrolling to the bottom reveals the next page (see the
// sentinel row in Listing.tsx); the full ranked list always exists in memory
// for the count text.
export const PAGE_SIZE = 250;

// Search results rendered at most, however many matched. Past the first
// hundred a rank has stopped telling the user anything they can act on, and
// the useful move is a better query rather than more scrolling — so the list
// stops here and the counter says how much it is not showing
// (listing/result-cap).
export const SEARCH_RESULT_CAP = 100;

// Above this many rendered rows the FLIP reorder animation is dropped. Measuring
// every row's offsetTop on each commit is one forced layout, but the per-row
// transform (a compositing layer each) is not free — on a listing this long the
// glide costs more than the snap it replaces.
export const FLIP_MAX_ROWS = 600;

// How long a row that just appeared in the folder keeps its tint. Long enough to
// catch the eye if you weren't looking at that part of the list, short enough
// that it doesn't become part of the row's normal appearance.
export const ROW_NEW_MS = 1500;

// Debounce for mirroring the query into the URL. Safari rate-limits
// history.replaceState (~100 calls / 30s, then it THROWS); per-keystroke
// sync trips that on fast typing. State stays immediate — only the URL lags.
export const URL_SYNC_MS = 200;

// Hits asked of the server per ranked query. The list renders at most
// SEARCH_RESULT_CAP of them; the rest are what makes the count chip ("top 100
// of 200+") true without a second request. 200 rows is a few KB.
export const SEARCH_RANK_LIMIT = 200;

// Hits asked of the server for a GLOB query. A glob's matches are all equally
// relevant — there is no tail to trim, so this is both the fetch limit and
// the render limit (capHits, listing/result-cap): a glob answer is never cut
// down to SEARCH_RESULT_CAP the way a substring answer is. 5,000 rows is the
// most a scrollable list and a select-all should ever try to hold at once.
export const SEARCH_GLOB_RANK_LIMIT = 5_000;

// How often the box re-asks while a scan covering the open folder is running.
// Results trickle in as the scan lands rows, which is the closest thing to
// live progress this search has; a finer poll would mostly re-read an index
// that has not changed, since a scan writes its rows in one compaction at the
// end.
export const SCAN_POLL_MS = 1_500;

export type ListingState =
  | { status: "loading" }
  // `truncated`: the directory has more entries than the server cap, so this
  // listing is a partial page. `cursor`: an opaque continuation token to fetch
  // the next page (non-null only on the resumable S3-direct route); null means
  // "no more can be fetched" — the banner then just states the listing is
  // partial without a Load more button.
  | { status: "ok"; entries: FsEntry[]; truncated: boolean; cursor: string | null }
  // `httpStatus`: the response code when the failure was an HTTP error, so
  // the view can tell a refused read (403 → AccessDenied) from the rest.
  | { status: "error"; message: string; httpStatus?: number };

// The in-folder search's answer state, in the shape the rendering keys off:
// nothing asked yet, a request out with nothing to show, a settled ranked
// answer (carrying the truncation the count chip owns up to), or a failure
// with nothing to show. Non-idle states are tagged with the `refresh`
// generation they were fetched for; `useListingSearch` treats a stale tag as
// idle, so a dir-watch bump invalidates the cache synchronously WITHOUT
// itself triggering a re-fetch.
export type SearchState =
  | { status: "idle" }
  | { status: "pending"; forRefresh: number }
  | { status: "ok"; truncated: boolean; total: number; forRefresh: number }
  | { status: "error"; message: string; forRefresh: number };

export const IDLE_SEARCH: SearchState = { status: "idle" };

// A ranked hit as the listing renders it. `positions` is recomputed on the
// client (listing/ranked-hits) rather than trusted off the wire — see that
// file's module comment.
export interface SearchHit {
  entry: WalkEntry;
  positions: number[];
}
