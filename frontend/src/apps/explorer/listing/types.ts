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

// Search-result rows rendered per "page". Fuzzy-scoring can match thousands
// of entries in a large tree; mounting them all as <tr>s at once is what jams
// the main thread (scoring itself is comparatively cheap). Scrolling to the
// bottom reveals the next page (see the sentinel row in Listing.tsx); the full
// ranked list always exists in memory for the count text.
export const PAGE_SIZE = 250;

// Above this many rendered rows the FLIP reorder animation is dropped. Measuring
// every row's offsetTop on each commit is one forced layout, but the per-row
// transform (a compositing layer each) is not free — on a listing this long the
// glide costs more than the snap it replaces.
export const FLIP_MAX_ROWS = 600;

// Minimum gap between commits of the RENDERED search ranking while a walk
// streams (see the throttle in useWalkSearch). Longer than STREAM_FLUSH_MS on
// purpose: that one bounds how often results are re-scored, this one bounds how
// often the rows on screen are allowed to move.
export const RERANK_COMMIT_MS = 280;

// How long a row that just appeared in the folder keeps its tint. Long enough to
// catch the eye if you weren't looking at that part of the list, short enough
// that it doesn't become part of the row's normal appearance.
export const ROW_NEW_MS = 1500;

// Debounce for mirroring the query into the URL. Safari rate-limits
// history.replaceState (~100 calls / 30s, then it THROWS); per-keystroke
// sync trips that on fast typing. State stays immediate — only the URL lags.
export const URL_SYNC_MS = 200;

// Minimum gap between streaming state flushes. Network chunks can arrive many
// times per second on localhost; committing (and re-scoring) on every one
// saturates the main thread and starves interaction. The first batch still
// flushes immediately (lastFlush starts at 0), so first paint isn't delayed.
export const STREAM_FLUSH_MS = 200;

export type ListingState =
  | { status: "loading" }
  // `truncated`: the directory has more entries than the server cap, so this
  // listing is a partial page. `cursor`: an opaque continuation token to fetch
  // the next page (non-null only on the resumable S3-direct route); null means
  // "no more can be fetched" — the banner then just states the listing is
  // partial without a Load more button.
  | { status: "ok"; entries: FsEntry[]; truncated: boolean; cursor: string | null }
  | { status: "error"; message: string };

// Streamed walk state. `entries` is one append-only array shared across the
// streaming updates (each batch pushes into it); every update still creates a
// NEW state object, so React re-renders and memos keyed on the walk recompute
// against the grown array. `count` is the running total (doubles as the
// version stamp that makes successive streaming states distinguishable).
// Non-idle states are tagged with the `refresh` generation they were fetched
// for; `validWalk` in useWalkSearch treats a stale tag as idle, so a dir-watch
// bump invalidates the cache synchronously WITHOUT itself triggering a
// re-fetch (fetching is driven by `walkReq` — see useWalkSearch). The
// component remounts per folder (keyed on fsPath in App), so no path tagging
// is needed.
export type WalkState =
  | { status: "idle" }
  | { status: "streaming"; entries: WalkEntry[]; count: number; forRefresh: number }
  | { status: "ok"; entries: WalkEntry[]; truncated: boolean; total: number; forRefresh: number }
  | { status: "error"; message: string; forRefresh: number };

export const IDLE_WALK: WalkState = { status: "idle" };

// Ranking: longest consecutive matched run first (a contiguous substring hit
// always beats a scattered subsequence one), then higher fuzzy score, then
// fewer path segments (shallower = closer to hand), then alphabetical for a
// stable order. Hits keep their score fields so partial result sets can be
// merged and re-sorted incrementally as the walk streams in.
export interface SearchHit {
  entry: WalkEntry;
  positions: number[];
  score: number;
  longestRun: number;
}
