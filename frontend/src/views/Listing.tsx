// Directory listing view with sortable columns and an in-folder search.
// Sort state lives in the URL (?sort=name|size|mtime&order=asc|desc) so a
// sorted listing is refresh-proof and bookmarkable like any other view state;
// the search query rides the URL the same way (?q=…). A non-empty query swaps
// the listing for flat, rank-ordered results over a recursive walk of the
// folder. The walk STREAMS (NDJSON batches, breadth-first from the server):
// results paint from the first batch and refine while deeper levels are still
// arriving, so feedback is instant even on huge trees. The walk starts lazily
// on first focus (or a URL-seeded query) and is cached until the dir watch
// fires.
import { useDeferredValue, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { navigate, navigateUrl, urlForFsPath, replaceSearch } from "../lib/router";
import {
  listDir,
  prefetchListDir,
  walkDirStream,
  revealPath,
  writeFile,
  mkdir,
  deleteEntry,
  renameEntry,
  copyEntry,
  statPath,
} from "../lib/api";
import type { FsEntry, WalkEntry } from "../lib/api";
import {
  dirname,
  normDir,
  join,
  freeDuplicatePath,
  freePastePath,
  copyToClipboard,
  clearClipboardIfDeleted,
  remapClipboardPath,
  pruneDescendantPaths,
  trashEntry,
  resolveOpenWithModes,
  buildOpenWithItems,
  friendlyFsError,
  claudeDeepLink,
} from "../lib/fs-actions";
import { acquireOverlay, releaseOverlay, isOverlayOpen } from "../lib/ui-overlay";
import { isMac, isMod } from "../lib/platform";
import { formatSize, formatMtime, basename } from "../lib/format";
import { fuzzyMatch, highlightSegments } from "../lib/fuzzy";
import { iconForEntry, isAppEntry } from "../components/FileIcons";
import { getViewState, setViewState } from "../lib/viewstate";
import { getClipboard, setClipboard, useClipboard } from "../lib/fs-clipboard";
import { pushToast } from "../lib/toast";
import ContextMenu, { type MenuEntry, type MenuItem } from "../components/ContextMenu";
import { MenuIcons } from "../components/MenuIcons";
import { PromptDialog, ConfirmDialog, nameError } from "../components/FsDialogs";
import { SplitRightIcon } from "../components/SplitIcons";
import ListingPreviewPane from "../components/ListingPreviewPane";

// A right-clicked row, normalized so both listing rows (name relative to the
// listed folder) and search-result rows (a `rel` path into a subtree) drive the
// same menu. `parentDir` is the containing folder; `path` is the entry itself.
interface RowCtx {
  path: string;
  name: string;
  isDir: boolean;
  parentDir: string;
}

// Row-shaped pruneDescendantPaths, for the batch row ops (Trash, Delete): a
// search selection can hold a folder row and rows from inside it, and removing
// the folder already removes those. Same input order.
function pruneDescendantRows(rows: RowCtx[]): RowCtx[] {
  const kept = new Set(pruneDescendantPaths(rows.map((r) => r.path)));
  return rows.filter((r) => kept.has(r.path));
}

// The target folder for a New File / Paste against a row: INTO a directory row,
// or the PARENT of a file row (Finder's behaviour).
function targetDirOf(row: RowCtx): string {
  return normDir(row.isDir ? row.path : row.parentDir);
}

// One open modal: a text prompt (New File/Folder, Rename) or a confirm (Delete).
type DialogState =
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

const SORT_KEYS = { name: "Name", size: "Size", mtime: "Modified" };
type SortKey = keyof typeof SORT_KEYS;
type SortOrder = "asc" | "desc";

// Search-result rows rendered per "page". Fuzzy-scoring can match thousands
// of entries in a large tree; mounting them all as <tr>s at once is what jams
// the main thread (scoring itself is comparatively cheap). Scrolling to the
// bottom reveals the next page (see the sentinel row below); the full ranked
// list always exists in memory for the count text.
const PAGE_SIZE = 250;

// Debounce for mirroring the query into the URL. Safari rate-limits
// history.replaceState (~100 calls / 30s, then it THROWS); per-keystroke
// sync trips that on fast typing. State stays immediate — only the URL lags.
const URL_SYNC_MS = 200;

// Minimum gap between streaming state flushes. Network chunks can arrive many
// times per second on localhost; committing (and re-scoring) on every one
// saturates the main thread and starves interaction. The first batch still
// flushes immediately (lastFlush starts at 0), so first paint isn't delayed.
const STREAM_FLUSH_MS = 200;

// Effective sort for a folder. An explicit `?sort` in the URL wins — a shared
// or hand-typed link is authoritative — otherwise fall back to this folder's
// own saved state (lib/viewstate), otherwise the default name/asc. So each
// folder shows its own remembered order regardless of how it was reached
// (clicked into, a breadcrumb, Back, or a fresh URL), and sibling folders keep
// independent sorts.
function resolveSort(fsPath: string): { sort: SortKey; order: SortOrder } {
  const url = new URLSearchParams(location.search);
  const src = url.get("sort") ? url : new URLSearchParams(getViewState(fsPath));
  const key = src.get("sort");
  const sort: SortKey = key && key in SORT_KEYS ? (key as SortKey) : "name";
  const order: SortOrder = src.get("order") === "desc" ? "desc" : "asc";
  return { sort, order };
}

// The preview pane's per-folder state, from the same viewstate querystring the
// sort rides in (keys `pane`/`panew` alongside `sort`/`order`). Deliberately
// NOT URL-synced (unlike sort): the pane is workspace layout, not view content
// a shared link should impose. Default off — a folder never toggled shows the
// plain listing exactly as before.
const PANE_MIN_W = 220;
const PANE_MAX_FRAC = 0.65;
const PANE_DEFAULT_W = 420;

function resolvePane(fsPath: string): { on: boolean; width: number } {
  const s = new URLSearchParams(getViewState(fsPath));
  const w = parseInt(s.get("panew") || "", 10);
  return { on: s.get("pane") === "1", width: Number.isFinite(w) && w >= PANE_MIN_W ? w : PANE_DEFAULT_W };
}

// Merge the pane keys into this folder's saved state without touching a saved
// sort (and vice versa — setSort merges the same way).
function savePaneState(fsPath: string, on: boolean, width: number): void {
  const s = new URLSearchParams(getViewState(fsPath));
  if (on) {
    s.set("pane", "1");
    s.set("panew", String(Math.round(width)));
  } else {
    s.delete("pane");
    s.delete("panew");
  }
  const qs = s.toString();
  setViewState(fsPath, qs ? "?" + qs : "");
}

function currentQuery(): string {
  return new URLSearchParams(location.search).get("q") || "";
}

// Shimmering placeholder rows shown while the listing fetch is in flight —
// same column shape as the real rows (icon + name + size + mtime), just with
// shimmer bars instead of text so the table never reads as "frozen". The
// width cycles make the bars ragged like real filenames.
const SKEL_NAME_W = [70, 45, 82, 38, 60, 50, 74, 42, 66, 34];
const SKEL_SIZE_W = [34, 28, 40, 24, 36, 30, 26, 38, 32, 22];
function skeletonRows(n: number): React.ReactNode {
  return Array.from({ length: n }, (_, i) => (
    <tr key={i} className="skel-row">
      <td className="name">
        <span className="skel-bar icon-skel" />
        <span className="skel-bar" style={{ width: `${SKEL_NAME_W[i % SKEL_NAME_W.length]}%` }} />
      </td>
      <td className="size">
        <span className="skel-bar" style={{ width: SKEL_SIZE_W[i % SKEL_SIZE_W.length] }} />
      </td>
      <td className="mtime">
        <span className="skel-bar" style={{ width: 84 }} />
      </td>
    </tr>
  ));
}

// A dot-leading query segment is explicit intent to SEE hidden entries.
// The walk itself always includes hidden entries (one dataset — the server
// prunes the actually-heavy machine trees like .git/node_modules, so hidden
// files are cheap to carry); this only gates whether dot-entries are shown.
// That makes ".py" work as an extension search (dotfiles like .pylintrc may
// match too — fine, they're real matches) without a second walk, and "env"
// deliberately not surface ".env".
function queryWantsHidden(rawQuery: string): boolean {
  const q = rawQuery.trim();
  return q.startsWith(".") || q.includes("/.");
}

// An entry is hidden when any path segment is dot-leading.
function isHiddenRel(rel: string): boolean {
  return rel.startsWith(".") || rel.includes("/.");
}

function sortEntries(entries: FsEntry[], sort: SortKey, order: SortOrder): FsEntry[] {
  const flip = order === "desc" ? -1 : 1;
  // Case-insensitive primary order, then an exact (case-sensitive) tiebreak so
  // names differing only by case/accent get a stable, deterministic order.
  // Without the tiebreak such names compare equal and the sort falls back to
  // the arbitrary os.listdir() arrival order, which changes between refreshes.
  const byName = (a: FsEntry, b: FsEntry) => {
    const c = a.name.localeCompare(b.name, undefined, { sensitivity: "base" });
    return c !== 0 ? c : a.name < b.name ? -1 : a.name > b.name ? 1 : 0;
  };
  return [...entries].sort((a, b) => {
    const aDot = a.name.startsWith(".");
    const bDot = b.name.startsWith(".");
    if (aDot !== bDot) return aDot ? 1 : -1; // dot entries always group last
    // Name sort is purely alphabetical — folders and files interleave. The
    // size/mtime sorts still group dirs first: a dir has no size and its mtime
    // means something different from a file's, so mixing them there is noise.
    if (sort !== "name" && a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
    let cmp: number;
    if (sort === "size") cmp = (a.size ?? -1) - (b.size ?? -1);
    else if (sort === "mtime") cmp = (a.mtime ?? 0) - (b.mtime ?? 0);
    else cmp = byName(a, b);
    if (cmp === 0) cmp = byName(a, b);
    return cmp * flip;
  });
}

// Ranking: longest consecutive matched run first (a contiguous substring hit
// always beats a scattered subsequence one), then higher fuzzy score, then
// fewer path segments (shallower = closer to hand), then alphabetical for a
// stable order. Hits keep their score fields so partial result sets can be
// merged and re-sorted incrementally as the walk streams in.
interface SearchHit {
  entry: WalkEntry;
  positions: number[];
  score: number;
  longestRun: number;
}

function rankCompare(a: SearchHit, b: SearchHit): number {
  if (b.longestRun !== a.longestRun) return b.longestRun - a.longestRun;
  if (b.score !== a.score) return b.score - a.score;
  const ad = a.entry.rel.split("/").length;
  const bd = b.entry.rel.split("/").length;
  if (ad !== bd) return ad - bd;
  return a.entry.rel.localeCompare(b.entry.rel, undefined, { sensitivity: "base" });
}

// Score `entries[from..]` against the query (unsorted — callers sort with
// rankCompare after merging). `showHidden=false` skips dot-entries before
// scoring (see queryWantsHidden). The `from` offset is what makes streaming
// cheap: each flush scores only the entries that arrived since the last one.
//
// On top of the fuzzy score, the entry NAME (last path segment) gets intent
// bonuses: an exact name match outranks everything ("Downloads" must beat
// "DownloadStage", whose extra camel-hump bonus otherwise wins), and a name
// starting with the query beats an interior hit. Char-level heuristics can't
// express "this IS the thing you typed", so it's layered here, not in fuzzy.ts.
function scoreEntries(query: string, entries: WalkEntry[], from: number, showHidden: boolean): SearchHit[] {
  const q = query.toLowerCase();
  const hits: SearchHit[] = [];
  for (let i = from; i < entries.length; i++) {
    const entry = entries[i];
    if (!showHidden && isHiddenRel(entry.rel)) continue;
    const m = fuzzyMatch(query, entry.rel);
    if (!m) continue;
    let score = m.score;
    const name = entry.rel.slice(entry.rel.lastIndexOf("/") + 1).toLowerCase();
    if (name === q) score += 100;
    else if (name.startsWith(q)) score += 25;
    hits.push({ entry, positions: m.positions, score, longestRun: m.longestRun });
  }
  return hits;
}

function renderHighlight(text: string, positions: number[]) {
  return highlightSegments(text, positions).map((seg, i) =>
    seg.match ? (
      <mark key={i} className="search-mark">
        {seg.text}
      </mark>
    ) : (
      <span key={i}>{seg.text}</span>
    )
  );
}

type ListingState =
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
// for; `validWalk` in the component treats a stale tag as idle, so a dir-watch
// bump invalidates the cache synchronously WITHOUT itself triggering a
// re-fetch (fetching is driven by `walkReq` — see below). The component
// remounts per folder (keyed on fsPath in App), so no path tagging is needed.
type WalkState =
  | { status: "idle" }
  | { status: "streaming"; entries: WalkEntry[]; count: number; forRefresh: number }
  | { status: "ok"; entries: WalkEntry[]; truncated: boolean; total: number; forRefresh: number }
  | { status: "error"; message: string; forRefresh: number };

const IDLE_WALK: WalkState = { status: "idle" };

// `provisional`: this Listing is rendering inside the pre-stat loading scaffold
// (App LoadingScaffold), mounted off a directory NAV HINT rather than a
// confirmed stat. The hint is authoritative in practice but can be stale — if
// the path is actually a file, /api/fs/list 404s. In that provisional phase a
// failed listing must NOT paint the hard "Failed to list" error: stat is still
// resolving and will drive the correct final view (a file <Preview>) a beat
// later, so we show the neutral loading body and let stat commit the real view.
// Absent/false (the committed post-stat render), errors show normally.
// Keyboard selection (the arrow-key row highlight) survives this component's
// per-folder remount. Opening a folder first paints App's pre-stat scaffold
// with a PROVISIONAL Listing, then swaps it for the resolved one when stat
// lands ~1s later — a remount that would otherwise wipe an in-progress arrow
// selection mid-keystroke (press Down during the open → the highlight vanishes).
// Like fs-clipboard, the state lives just outside the remount boundary. A single
// entry keyed by fsPath is enough (only one Listing is mounted at a time): it
// bridges the scaffold→resolved swap and restores the highlight if you browse
// back to the same folder.
//
// The stash carries the WHOLE multi-selection (see Selection below), not just
// one path, so a Shift-range built in the provisional Listing survives too.
interface Selection {
  // Selected row paths, in the order they entered the selection (a Shift-range
  // enters in rendered row order). Empty = nothing selected.
  paths: string[];
  // Where a Shift-range extends FROM: the last row selected by a plain or
  // Mod-click / plain arrow move. null when nothing has been selected yet.
  anchor: string | null;
  // The focused row: what arrows move from, what Enter opens, what F2 renames,
  // and what a paste/new-file targets. null = no selection.
  lead: string | null;
}

const EMPTY_SELECTION: Selection = { paths: [], anchor: null, lead: null };

// Collapse to exactly one row — the plain-click / plain-arrow / re-anchor case.
function oneSelected(path: string): Selection {
  return { paths: [path], anchor: path, lead: path };
}

let lastSelection: { fsPath: string; sel: Selection } | null = null;
function recallSelection(fsPath: string): Selection {
  return lastSelection && lastSelection.fsPath === fsPath ? lastSelection.sel : EMPTY_SELECTION;
}
function rememberSelection(fsPath: string, sel: Selection): void {
  lastSelection = { fsPath, sel };
}

// A contiguous range of rendered rows, inclusive, in row order. `rows` is the
// live navRows order (the SORTED/rendered order, never the raw fs order).
function rangeBetween(rows: string[], from: string, to: string): string[] {
  const a = rows.indexOf(from);
  const b = rows.indexOf(to);
  if (a === -1 || b === -1) return b === -1 ? [] : [to];
  return a <= b ? rows.slice(a, b + 1) : rows.slice(b, a + 1);
}

// How many rows a PageUp/PageDown moves: one viewport of rows minus one, so the
// row you were on stays visible as context. Measured from the live DOM (scroller
// height / row height) and falls back to a sane constant before first paint.
const PAGE_ROWS_FALLBACK = 12;
function pageRows(): number {
  const scroller = document.querySelector(".listing-scroll") as HTMLElement | null;
  const row = document.querySelector("table.listing-table tr.row") as HTMLElement | null;
  const rowH = row?.offsetHeight ?? 0;
  if (!scroller || rowH <= 0) return PAGE_ROWS_FALLBACK;
  return Math.max(1, Math.floor(scroller.clientHeight / rowH) - 1);
}

// Plural-friendly name for a batch of rows, used both in menu labels and as the
// `name` in a friendlyFsError context ("Couldn't duplicate \"3 items\"").
function batchLabel(rows: { name: string }[]): string {
  return rows.length === 1 ? rows[0].name : `${rows.length} items`;
}

export default function Listing({
  fsPath,
  provisional = false,
  onSingleApp,
}: {
  fsPath: string;
  provisional?: boolean;
  // Reports the path of this directory's lone top-level HTML file (an
  // "app"), or null when there isn't exactly one — the caller (Preview's
  // header) uses this to surface an "Open as app" button. Fires whenever the
  // plain (non-search) listing settles, so it tracks dir-watch refreshes too.
  onSingleApp?: (path: string | null) => void;
}) {
  const [state, setState] = useState<ListingState>({ status: "loading" });
  // Sort lives in the URL; mirror it in state so clicks re-render without a
  // navigation (vanilla re-ran renderListing after its replaceState).
  const [{ sort, order }, setSortState] = useState<{ sort: SortKey; order: SortOrder }>(() =>
    resolveSort(fsPath)
  );
  // When the sort was restored from saved state (URL carried none), reflect it
  // in the URL so the address bar, bookmarks, and Back-button history match
  // what's shown — as if the column had been clicked. Only syncs a genuinely
  // saved order; an unsorted folder keeps its clean, param-free URL. replaceState
  // (not navigate) so the view doesn't remount.
  useEffect(() => {
    if (new URLSearchParams(location.search).get("sort")) return; // URL is authoritative
    const s = new URLSearchParams(getViewState(fsPath));
    // No stored SORT → leave default sort + clean URL. (The stored string may
    // still carry pane keys — those never ride the URL.)
    if (!s.get("sort")) return;
    const params = new URLSearchParams(location.search);
    params.set("sort", s.get("sort") || "name");
    params.set("order", s.get("order") === "desc" ? "desc" : "asc");
    replaceSearch(location.pathname + "?" + params.toString());
  }, [fsPath]);
  const [refresh, setRefresh] = useState(0); // bumped by the dir watch socket
  // loadMore captures the refresh generation it started in; a dir-watch refresh
  // on the SAME path (App keys StatView on epoch+fsPath, so cross-directory
  // merges can't happen, but a same-path re-fetch can) resets the listing while
  // a cursored fetch is pending — the stale page must be discarded, not merged
  // into the refreshed listing. Ref so the async callback reads the LATEST
  // generation, not the one captured when loadMore was defined.
  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;
  const [query, setQueryState] = useState<string>(currentQuery);
  const [walk, setWalk] = useState<WalkState>(IDLE_WALK);
  // Which refresh generation of the walk has been REQUESTED (null = none).
  // The fetch effect keys on this, not on `refresh` itself: a dir-watch bump
  // only invalidates the cache (via the forRefresh tag) and a new fetch
  // happens only while search is active (auto-request effect) or on the next
  // gesture — an idle listing must not re-walk the tree on every watch event.
  const [walkReq, setWalkReq] = useState<number | null>(() =>
    currentQuery().trim() !== "" ? 0 : null
  );
  // Bumped to re-run the stream effect after an error, from a real user
  // gesture only (focus / typing) — an effect-driven retry would loop forever.
  const [retryNonce, setRetryNonce] = useState(0);
  // Sort applied to search results. null = relevance (fuzzy rank). Deliberately
  // NOT URL-synced (unlike the normal-mode sort) — it resets on every query
  // change, so persisting it would fight that reset.
  const [searchSort, setSearchSort] = useState<{ sort: SortKey; order: SortOrder } | null>(null);
  // How many result rows are revealed; grows by PAGE_SIZE when the sentinel
  // row scrolls into view, resets on every query change.
  const [visibleCount, setVisibleCount] = useState(PAGE_SIZE);
  // The selected rows (see Selection): one for a plain click / arrow move, many
  // for a Shift-range, Mod-click toggle or Select All. Seeded from the
  // cross-remount store so a selection made in the pre-stat provisional Listing
  // survives the swap to the resolved one (see recallSelection / lastSelection).
  const [sel, setSel] = useState<Selection>(() => recallSelection(fsPath));
  // The lead row — every place that used to read `selectedPath` (scroll-into-
  // view, reconcile, Enter/F2 targets) still works off this single path.
  const selectedPath = sel.lead;
  // A Load more fetch (next page of a truncated listing) is in flight.
  const [loadingMore, setLoadingMore] = useState(false);

  // --- Preview pane (right-hand split) ---------------------------------------
  // Visibility + width restore from this folder's saved viewstate (same store
  // as the sort — see resolvePane); both persist on change. Width is clamped
  // live during the divider drag; the max fraction is enforced against the
  // split container's current size.
  const [pane, setPane] = useState<{ on: boolean; width: number }>(() => resolvePane(fsPath));
  const splitRef = useRef<HTMLDivElement>(null);
  const togglePane = () => {
    setPane((prev) => {
      const next = { ...prev, on: !prev.on };
      savePaneState(fsPath, next.on, next.width);
      return next;
    });
  };
  // The divider drag: pointer capture keeps the drag alive when the cursor
  // crosses into the pane's iframe (which would otherwise swallow mousemove).
  const onDividerPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    const divider = e.currentTarget;
    divider.setPointerCapture(e.pointerId);
    divider.classList.add("dragging");
    let width = pane.width;
    const onMove = (ev: PointerEvent) => {
      const rect = splitRef.current?.getBoundingClientRect();
      if (!rect) return;
      // The pane is the right side: its width is the distance from the cursor
      // to the container's right edge, clamped to [min, max fraction].
      width = Math.max(PANE_MIN_W, Math.min(rect.width * PANE_MAX_FRAC, rect.right - ev.clientX));
      setPane((prev) => (prev.width === width ? prev : { ...prev, width }));
    };
    const onUp = () => {
      divider.classList.remove("dragging");
      divider.removeEventListener("pointermove", onMove);
      divider.removeEventListener("pointerup", onUp);
      divider.removeEventListener("pointercancel", onUp);
      savePaneState(fsPath, true, width);
    };
    divider.addEventListener("pointermove", onMove);
    divider.addEventListener("pointerup", onUp);
    divider.addEventListener("pointercancel", onUp);
  };

  // --- Context-menu / file-operation state ----------------------------------
  // The open context menu (position + items) and the open modal, both local to
  // this folder view. Toasts are NOT local: they go to the global store
  // (lib/toast), which owns the queue, the auto-dismiss timer and the single
  // bottom-right stack every notification shares. In panel/tab mode each pane
  // is its own document, so a pane's toast still lands in that pane's corner.
  // The cut/copy clipboard is likewise a module-level store (lib/fs-clipboard)
  // so it survives this component's per-folder remount (see there).
  const clipboard = useClipboard();
  const [menu, setMenu] = useState<{ x: number; y: number; items: MenuEntry[] } | null>(null);
  const [dialog, setDialog] = useState<DialogState | null>(null);

  // Search input, so a keystroke anywhere in the listing can focus it.
  const searchInputRef = useRef<HTMLInputElement>(null);
  // Latest ordered list of navigable row paths + the current selection, read by
  // the document keydown handler (registered once, so it can't close over them).
  const navRowsRef = useRef<string[]>([]);
  // Same mirroring pattern as before, widened to the whole selection: the
  // document handlers are registered ONCE (empty deps), so they read current
  // selection through this ref instead of re-registering per change.
  const selRef = useRef<Selection>(sel);
  selRef.current = sel;
  // Fast membership test for the row renderer (a Select All can hold thousands).
  const selectedSet = useMemo(() => new Set(sel.paths), [sel.paths]);
  // Mirror the selection into the cross-remount store so it's already there
  // when the resolved Listing mounts (the provisional one has no unmount step
  // that would clear it). Keyed by fsPath, so a real nav to another folder
  // starts fresh.
  useEffect(() => {
    rememberSelection(fsPath, sel);
  }, [fsPath, sel]);
  // Path -> RowCtx for the rendered rows, read by the once-registered keydown
  // handler so Enter can pass the row's is_dir as a nav hint (see rowCtxByPath
  // below, which assigns this each render).
  const rowCtxByPathRef = useRef<Map<string, RowCtx>>(new Map());
  // True while a context menu or a modal dialog is open. The document-level nav
  // and shortcut handlers (registered once, reading refs) hard-guard on this so
  // an open overlay owns the keyboard — a stray Enter can't navigate a row and
  // Cmd+Backspace can't trash one behind the dialog, regardless of where focus
  // sits (the dialog's own containment covers focus; this covers the rest).
  const overlayOpenRef = useRef(false);
  overlayOpenRef.current = menu !== null || dialog !== null;
  // Also publish this view's overlay state to the shared registry (lib/
  // ui-overlay) so OTHER views back off. When a directory is opened in Preview,
  // that Preview's header menu/dialogs live in separate state; this embedded
  // Listing's document-level handlers must not fire behind them (and vice
  // versa). acquire on open, release on close — and on unmount, so a nav-away
  // while the menu is open can't leak a held count.
  // Layout effect so the hold registers before paint — no one-frame window
  // where another view's handlers still see isOverlayOpen() === false.
  useLayoutEffect(() => {
    if (!overlayOpenRef.current) return;
    acquireOverlay();
    return () => releaseOverlay();
  }, [menu, dialog]);
  // A path the selection should jump to once it appears in the reloaded rows
  // (a rename/duplicate target — its row doesn't exist until the refetch lands).
  const pendingSelectRef = useRef<string | null>(null);
  // Last known index of the selection within navRows. When the selected path
  // vanishes (delete / move to bin / rename with no re-anchor) the reconcile
  // effect clamps to this slot so selection lands on the nearest surviving row.
  const lastSelIndexRef = useRef<number>(-1);

  // --- selection mutators ---------------------------------------------------
  // Every one of these closes over nothing but setSel and navRowsRef (both
  // stable for the component's life), so the once-registered document handlers
  // below can safely capture them from the first render.

  const selectOnly = (path: string) => setSel(oneSelected(path));

  const clearSelection = () => setSel(EMPTY_SELECTION);

  // Mod-click: add/remove one row, and make it the anchor a later Shift-range
  // pivots on (Finder/Explorer both re-anchor on the toggled row).
  const toggleSelected = (path: string) =>
    setSel((prev) => {
      if (!prev.paths.includes(path)) {
        return { paths: [...prev.paths, path], anchor: path, lead: path };
      }
      const paths = prev.paths.filter((p) => p !== path);
      // Deselecting the lead hands focus to whatever is left of the selection.
      return { paths, anchor: path, lead: paths.length ? paths[paths.length - 1] : null };
    });

  // Shift-click / Shift+arrow: the selection becomes anchor..path over the
  // RENDERED row order (navRows — the active sort or search ranking), with the
  // anchor left in place so further extension keeps pivoting on it.
  const extendTo = (path: string) =>
    setSel((prev) => {
      const anchor = prev.anchor ?? prev.lead;
      if (anchor === null) return oneSelected(path);
      const paths = rangeBetween(navRowsRef.current, anchor, path);
      if (!paths.length) return prev;
      return { paths, anchor, lead: path };
    });

  const selectAllRows = () =>
    setSel((prev) => {
      const rows = navRowsRef.current;
      if (!rows.length) return prev;
      return {
        paths: [...rows],
        anchor: prev.lead ?? rows[0],
        lead: prev.lead ?? rows[rows.length - 1],
      };
    });

  // Move the lead to `index` (clamped into the row range), either collapsing the
  // selection onto that row or extending the range from the anchor.
  const moveLeadTo = (index: number, extend: boolean) =>
    setSel((prev) => {
      const rows = navRowsRef.current;
      if (!rows.length) return prev;
      const next = rows[Math.max(0, Math.min(rows.length - 1, index))];
      if (!extend) return oneSelected(next);
      const anchor = prev.anchor ?? prev.lead ?? next;
      return { paths: rangeBetween(rows, anchor, next), anchor, lead: next };
    });

  // Keyboard navigation for the listing, whether focus is in the search box or
  // nowhere in particular:
  //   • a plain printable key focuses the search box so the character lands there;
  //   • Up/Down move the selection through the rendered rows — in the search box
  //     too, since a single-line input doesn't need them for the caret — and
  //     Shift+Up/Down extend the range from the anchor instead;
  //   • Home/End jump to the first/last row, PageUp/PageDown move a viewport
  //     (both extend with Shift, like every list widget);
  //   • Mod+A selects every rendered row, Escape clears the selection;
  //   • Enter opens the lead row, or the top row when nothing is selected yet.
  // Modifier chords that are NOT selection movement (Mod+Up/Down = parent/open)
  // are deliberately left to the shortcut handler further down.
  // Bound to `document` so it also drives the plain listing with nothing focused.
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      // While an IME is composing, Enter confirms a candidate and the arrows
      // move through the candidate list — never repurpose them for navigation.
      if (e.isComposing) return;
      // An open context menu / dialog owns the keyboard: don't let Enter open a
      // row behind it (the dialog handles its own Enter/Escape). isOverlayOpen()
      // also covers an overlay owned by a HOSTING view (Preview's header menu
      // when this Listing is embedded), which overlayOpenRef alone can't see.
      if (overlayOpenRef.current || isOverlayOpen()) return;
      const el = document.activeElement as HTMLElement | null;
      const inSearch = el === searchInputRef.current;
      // Only drive navigation from the search box or when nothing in particular
      // is focused (body). If focus is on a chrome control — a breadcrumb link,
      // the bookmark/mode-switch buttons, another input — leave its keys alone
      // (otherwise Enter would open a file instead of activating that control).
      const navActive =
        inSearch || !el || el === document.body || el === document.documentElement;

      const rows = navRowsRef.current;
      const leadIdx = rows.indexOf(selRef.current.lead ?? "");

      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        if (!navActive) return;
        // Mod+Up/Down are navigation chords (parent folder / open), owned by the
        // shortcut handler — they must not also move the selection.
        if (isMod(e) || e.altKey) return;
        if (!rows.length) return;
        e.preventDefault();
        const down = e.key === "ArrowDown";
        // Nothing selected yet: Down starts at the top, Up at the bottom.
        const next = leadIdx === -1 ? (down ? 0 : rows.length - 1) : leadIdx + (down ? 1 : -1);
        moveLeadTo(next, e.shiftKey);
        return;
      }
      if (e.key === "Home" || e.key === "End") {
        // Unlike Up/Down, Home/End are real caret navigation in a text field, so
        // the search box keeps them (same carve-out as Mod+A and Escape below).
        if (!navActive || inSearch || isMod(e) || !rows.length) return;
        e.preventDefault();
        moveLeadTo(e.key === "Home" ? 0 : rows.length - 1, e.shiftKey);
        return;
      }
      if (e.key === "PageDown" || e.key === "PageUp") {
        if (!navActive || isMod(e) || !rows.length) return;
        e.preventDefault();
        const step = pageRows();
        const down = e.key === "PageDown";
        const next = leadIdx === -1 ? (down ? 0 : rows.length - 1) : leadIdx + (down ? step : -step);
        moveLeadTo(next, e.shiftKey);
        return;
      }
      if (isMod(e) && e.key.toLowerCase() === "a") {
        // Select All. In the search box it must keep meaning "select the text",
        // otherwise clearing a typed query becomes impossible.
        if (!navActive || inSearch || !rows.length) return;
        e.preventDefault();
        selectAllRows();
        return;
      }
      if (e.key === "Escape") {
        // Clear the selection. The search input owns Escape while focused (it
        // clears the query — see its onKeyDown), and the overlay/dialog guards
        // above already stopped us if anything modal is up.
        //
        // A pending copy/cut outranks the selection: App's capture-phase Escape
        // handler cancels the clipboard and calls preventDefault(), so one press
        // never does both. Reading defaultPrevented keeps that precedence here
        // without a second copy of the clipboard logic (which would also be
        // wrong — the cancel has to work from Preview, where no Listing exists).
        if (e.defaultPrevented) return;
        if (!navActive || inSearch) return;
        if (!selRef.current.paths.length) return;
        e.preventDefault();
        clearSelection();
        return;
      }
      if (e.key === "Enter") {
        // Already consumed by a chrome control that unmounted itself on the
        // way (the breadcrumb's path input commits and closes on Enter, which
        // hands focus back to <body> before this listener runs — navActive
        // alone can't see that the key was spoken for).
        if (e.defaultPrevented) return;
        if (!navActive) return;
        if (!rows.length) return;
        e.preventDefault();
        const target = leadIdx === -1 ? rows[0] : rows[leadIdx];
        navigate(target, { isDir: rowCtxByPathRef.current.get(target)?.isDir });
        return;
      }
      // Start typing → focus the search box so the character lands there. Only
      // when nothing else is focused (not the search box already, not a chrome
      // control) and only plain printable keys (no modifiers), so Space on a
      // focused button and app shortcuts keep working.
      if (
        navActive && !inSearch &&
        e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey
      ) {
        searchInputRef.current?.focus(); // keystroke falls through into the input
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);

  // The input echoes `query` (immediate) so keystrokes never wait on the
  // fuzzy-scoring/rendering work below. `deferredQuery` trails behind under
  // load — React commits a cheap render with the old deferred value first
  // (echoing the keystroke), then a low-priority render picks up the new
  // value and redoes the expensive work, interruptible by further typing.
  const deferredQuery = useDeferredValue(query);
  const q = deferredQuery.trim();
  const searching = q !== "";
  const isStale = query.trim() !== q;

  useEffect(() => {
    let alive = true;
    // A fresh fetch (navigation or dir-watch refresh) resets any accumulated
    // Load more pages: the new listing replaces the array wholesale.
    setLoadingMore(false);
    // Initial mount goes through the prefetch cache so a listing kicked off by
    // the loading scaffold (in parallel with stat) is reused when the real
    // preview remounts this component for the same path — no duplicate request.
    // A dir-watch refresh (refresh > 0) must see live data, so it bypasses.
    (refresh === 0 ? prefetchListDir(fsPath) : listDir(fsPath)).then(
      (data) =>
        alive &&
        setState({
          status: "ok",
          entries: data.entries,
          truncated: !!data.truncated,
          cursor: data.cursor ?? null,
        }),
      (err: Error) => alive && setState({ status: "error", message: err.message })
    );
    return () => {
      alive = false;
    };
  }, [fsPath, refresh]);

  // Fetch the next page of a truncated S3-direct listing and APPEND it (dedupe
  // by name). The accumulated set is still sorted by the active column below —
  // honest because the banner states the listing is partial; a global sort over
  // the WHOLE directory is impossible (we only ever hold fetched pages).
  const loadMore = () => {
    if (state.status !== "ok" || !state.cursor || loadingMore) return;
    const cursor = state.cursor;
    const gen = refresh; // discard the response if a refresh supersedes it
    setLoadingMore(true);
    listDir(fsPath, cursor).then(
      (data) => {
        if (refreshRef.current !== gen) return; // stale: a refresh replaced the listing
        setLoadingMore(false);
        setState((prev) => {
          if (prev.status !== "ok") return prev;
          const seen = new Set(prev.entries.map((e) => e.name));
          const merged = prev.entries.concat(
            data.entries.filter((e) => !seen.has(e.name))
          );
          return {
            status: "ok",
            entries: merged,
            truncated: !!data.truncated,
            cursor: data.cursor ?? null,
          };
        });
      },
      (err: Error) => {
        if (refreshRef.current !== gen) return; // stale: the fetch effect reset state
        setLoadingMore(false);
        pushToast({ msg: err.message, tone: "error" });
      }
    );
  };

  // WebSocket watch on the listed directory (LS-1); WS not SSE per D74 (SSE
  // pinned one of Chrome's 6 HTTP/1.1 sockets per view). A directory's mtime
  // changes on create/delete/rename of entries (not on child content changes
  // — LS-2, accepted). Closed on unmount = navigating away (LS-3). On change,
  // debounce 300 ms then re-fetch; sort params live in URL + state, so a
  // refetch preserves them.
  useEffect(() => {
    let sock: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let closed = false;
    const connect = () => {
      const proto = location.protocol === "https:" ? "wss://" : "ws://";
      sock = new WebSocket(proto + location.host + "/api/fs/events?path=" + encodeURIComponent(fsPath));
      sock.onmessage = (ev) => {
        let data;
        try {
          data = JSON.parse(ev.data);
        } catch {
          return;
        }
        if (data.keepalive) return;
        if (timer !== null) clearTimeout(timer);
        timer = setTimeout(() => setRefresh((n) => n + 1), 300);
      };
      // WebSockets don't auto-reconnect the way EventSource did.
      sock.onclose = () => {
        if (!closed) retry = setTimeout(connect, 1000);
      };
    };
    connect();
    return () => {
      closed = true;
      if (retry !== null) clearTimeout(retry);
      if (timer !== null) clearTimeout(timer);
      sock?.close();
    };
  }, [fsPath]);

  // Synchronous cache validity: a non-idle walk fetched for a previous
  // refresh generation reads as idle, immediately on the render where
  // `refresh` bumps — no effect ordering to wait on, and no render ever
  // scores search results against the pre-refresh tree.
  const validWalk: WalkState =
    walk.status === "idle" || walk.forRefresh === refresh ? walk : IDLE_WALK;

  // Active search must always have a walk for the CURRENT tree. Covers a
  // URL-seeded query on mount racing ahead of focus, typing after an
  // invalidation, and the dir watch bumping `refresh` mid-search (the stale
  // tag makes validWalk idle, this re-requests). Keyed on validWalk being
  // IDLE so an errored walk never auto-retries (that would loop:
  // request -> error -> request -> ...); error retries hang off real
  // gestures (focus / typing) below. The immediate `query` (not deferred)
  // drives this — the fetch should start on the first keystroke.
  useEffect(() => {
    if (query.trim() !== "" && validWalk.status === "idle" && walkReq !== refresh) {
      setWalkReq(refresh);
    }
  }, [query, validWalk.status, walkReq, refresh]);

  // The streamed validWalk. One effect owns the whole fetch lifecycle: it runs
  // when a walk generation is requested (walkReq) or a gesture bumps
  // `retryNonce` after an error, and ABORTS the in-flight stream on cleanup
  // — which also cancels the server-side walk (the generator is closed on
  // disconnect). Batches push into one append-only array; see WalkState.
  useEffect(() => {
    if (walkReq === null) return;
    const forRefresh = walkReq;
    const ctrl = new AbortController();
    let alive = true;
    const entries: WalkEntry[] = [];
    // Flush throttle (STREAM_FLUSH_MS): entries accumulate in `pending`
    // between commits so the scoring/render work runs a few times a second,
    // not once per network chunk. A trailing timer guarantees the last
    // partial interval still commits.
    let pending: WalkEntry[] = [];
    let lastFlush = 0;
    let flushTimer: ReturnType<typeof setTimeout> | null = null;
    const flush = () => {
      if (flushTimer !== null) {
        clearTimeout(flushTimer);
        flushTimer = null;
      }
      for (const e of pending) entries.push(e); // no spread: a big chunk would blow the arg limit
      pending = [];
      lastFlush = Date.now();
      setWalk({ status: "streaming", entries, count: entries.length, forRefresh });
    };
    setWalk({ status: "streaming", entries, count: 0, forRefresh });
    walkDirStream(fsPath, {
      hidden: true,
      signal: ctrl.signal,
      onBatch: (batch) => {
        if (!alive) return;
        for (const e of batch) pending.push(e);
        const wait = STREAM_FLUSH_MS - (Date.now() - lastFlush);
        if (wait <= 0) flush();
        else if (flushTimer === null) flushTimer = setTimeout(() => alive && flush(), wait);
      },
    }).then(
      (end) => {
        if (!alive) return;
        if (flushTimer !== null) clearTimeout(flushTimer);
        for (const e of pending) entries.push(e);
        setWalk({ status: "ok", entries, truncated: end.truncated, total: end.total, forRefresh });
      },
      (err: Error) => {
        if (!alive || err.name === "AbortError") return;
        if (flushTimer !== null) clearTimeout(flushTimer);
        setWalk({ status: "error", message: err.message, forRefresh });
      }
    );
    return () => {
      alive = false;
      if (flushTimer !== null) clearTimeout(flushTimer);
      ctrl.abort();
    };
  }, [fsPath, walkReq, retryNonce]);

  // First focus starts the walk warming in the background; focus (like
  // typing below) is also the retry gesture when a previous stream failed.
  const prefetchWalk = () => {
    if (validWalk.status === "idle") setWalkReq(refresh);
    else if (validWalk.status === "error") {
      setWalkReq(refresh);
      setRetryNonce((n) => n + 1);
    }
  };

  // Debounced URL mirror for the query (see URL_SYNC_MS). Pending sync is
  // dropped on unmount — a navigation has already replaced the URL by then.
  const urlTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (urlTimer.current !== null) clearTimeout(urlTimer.current);
    },
    []
  );

  const setQuery = (value: string) => {
    setQueryState(value);
    setSearchSort(null); // a new query drops back to relevance order
    setVisibleCount(PAGE_SIZE);
    // Editing the query is also a user gesture: if the last walk attempt
    // failed, give it another shot instead of leaving search dead forever.
    // (An idle walk needs no handling here — the auto-request effect fires
    // as soon as the non-empty query state lands.)
    if (validWalk.status === "error") {
      setWalkReq(refresh);
      setRetryNonce((n) => n + 1);
    }
    if (urlTimer.current !== null) clearTimeout(urlTimer.current);
    urlTimer.current = setTimeout(() => {
      const params = new URLSearchParams(location.search);
      if (value) params.set("q", value);
      else params.delete("q");
      const qs = params.toString();
      replaceSearch(location.pathname + (qs ? "?" + qs : ""));
    }, URL_SYNC_MS);
  };

  const setSort = (key: SortKey) => {
    const next: { sort: SortKey; order: SortOrder } = {
      sort: key,
      order: key === sort && order === "asc" ? "desc" : "asc",
    };
    const params = new URLSearchParams(location.search);
    params.set("sort", next.sort);
    params.set("order", next.order);
    replaceSearch(location.pathname + "?" + params.toString());
    setSortState(next);
    // Remember this folder's choice so returning to it later restores this sort.
    // Only sort/order are persisted — the in-folder search `q` stays transient.
    // Merged into the saved string so the pane keys (resolvePane) survive.
    const saved = new URLSearchParams(getViewState(fsPath));
    saved.set("sort", next.sort);
    saved.set("order", next.order);
    setViewState(fsPath, "?" + saved.toString());
  };

  const setSearchSortKey = (key: SortKey) => {
    setSearchSort((prev) =>
      prev && prev.sort === key
        ? { sort: key, order: prev.order === "asc" ? "desc" : "asc" }
        : { sort: key, order: "asc" }
    );
  };

  // Incremental-scoring cache for the streamed validWalk. As long as the query,
  // hidden-intent and entries array are unchanged, only entries appended
  // since `scored` get fuzzy-matched, then merged into the previous ranked
  // list — so a stream flush near the tail of a 200k walk costs one small
  // scan + a sort of the hits, not a full re-scan of everything (which is
  // exactly what saturated the main thread and made the UI unresponsive
  // while the walk loaded). Any change to query/hidden/array falls back to a
  // full scan. A ref (not state): it's a pure memo accelerator, and the
  // update below is idempotent, so double-invoked renders are harmless.
  const scoreCache = useRef<{
    q: string;
    showHidden: boolean;
    entries: WalkEntry[] | null;
    scored: number; // how many of `entries` have been scored already
    ranked: SearchHit[];
  }>({ q: "", showHidden: false, entries: null, scored: 0, ranked: [] });

  // Keyed on `q`/`searching` (both deferred) so full fuzzy scans run on
  // React's low-priority schedule, not synchronously on every keystroke.
  // While the walk streams, each flush produces a new `walk` state and this
  // extends the ranked list with just the newly arrived entries (see
  // scoreCache above).
  const hits = useMemo(() => {
    if (!searching || (validWalk.status !== "ok" && validWalk.status !== "streaming")) return [];
    const showHidden = queryWantsHidden(q);
    const cache = scoreCache.current;
    let ranked: SearchHit[];
    if (cache.entries === validWalk.entries && cache.q === q && cache.showHidden === showHidden) {
      const fresh = scoreEntries(q, validWalk.entries, cache.scored, showHidden);
      ranked = fresh.length ? cache.ranked.concat(fresh).sort(rankCompare) : cache.ranked;
    } else {
      ranked = scoreEntries(q, validWalk.entries, 0, showHidden).sort(rankCompare);
    }
    scoreCache.current = { q, showHidden, entries: validWalk.entries, scored: validWalk.entries.length, ranked };
    if (!searchSort) return ranked; // relevance order
    const { sort, order } = searchSort;
    const flip = order === "desc" ? -1 : 1;
    const byName = (a: SearchHit, b: SearchHit) =>
      a.entry.rel.localeCompare(b.entry.rel, undefined, { sensitivity: "base" });
    return [...ranked].sort((a, b) => {
      let cmp: number;
      if (sort === "size") cmp = (a.entry.size ?? -1) - (b.entry.size ?? -1);
      else if (sort === "mtime") cmp = (a.entry.mtime ?? 0) - (b.entry.mtime ?? 0);
      else cmp = byName(a, b);
      if (cmp === 0) cmp = byName(a, b);
      return cmp * flip;
    });
  }, [searching, q, validWalk, searchSort]);

  const visibleHits = useMemo(() => hits.slice(0, visibleCount), [hits, visibleCount]);

  // Reveal the next page when the sentinel row (rendered only while more rows
  // exist) scrolls into view. rootMargin pre-triggers a bit before the bottom
  // so the next page is usually mounted by the time the user reaches it.
  const sentinelRef = useRef<HTMLTableRowElement | null>(null);
  const hasMore = searching && hits.length > visibleCount;
  useEffect(() => {
    const el = sentinelRef.current;
    if (!el || !hasMore) return;
    const io = new IntersectionObserver(
      (obsEntries) => {
        if (obsEntries.some((e) => e.isIntersecting)) setVisibleCount((c) => c + PAGE_SIZE);
      },
      { root: el.closest(".listing-scroll"), rootMargin: "200px" }
    );
    io.observe(el);
    return () => io.disconnect();
  }, [hasMore, visibleCount]);

  // Same idea for the plain (non-search) listing: re-sorting on every render
  // (e.g. a keystroke that flips `searching` before this branch even
  // displays) was pure waste when `state`/sort/order hadn't changed.
  const sortedEntries = useMemo(
    () => (state.status === "ok" ? sortEntries(state.entries, sort, order) : []),
    [state, sort, order]
  );

  const base = fsPath.replace(/\/$/, "");

  // Tell the caller whether this folder's top level holds exactly one HTML
  // ("app") file. Keyed off the plain listing, not the search results — the
  // button this drives describes the folder's own contents, regardless of
  // what's currently typed into the in-folder search box.
  //   • A truncated listing (the server-cap banner) only ever holds a partial
  //     page, so a lone HTML match there doesn't mean it's the folder's only
  //     one — withhold the report rather than risk a false "app" button.
  //   • "loading" reports nothing either way (neither null nor a path) so a
  //     same-path remount (e.g. switching a mode away from `_listing` and
  //     back) doesn't flicker an already-known button off for the length of
  //     the refetch; only "ok"/"error" settle the caller's state.
  useEffect(() => {
    if (!onSingleApp) return;
    if (state.status === "loading") return;
    if (state.status !== "ok" || state.truncated) {
      onSingleApp(null);
      return;
    }
    const apps = state.entries.filter((e) => isAppEntry(e.name, e.is_dir));
    onSingleApp(apps.length === 1 ? base + "/" + apps[0].name : null);
  }, [state, base, onSingleApp]);

  // Flat, ordered list of the paths the arrow keys step through: the rendered
  // search hits while searching, otherwise the sorted listing. Keyed off the
  // same memoized arrays the table renders, so selection never drifts from view.
  const navRows = useMemo(
    () =>
      searching
        ? visibleHits.map(({ entry }) => base + "/" + entry.rel)
        : sortedEntries.map((entry) => base + "/" + entry.name),
    [searching, visibleHits, sortedEntries, base]
  );
  navRowsRef.current = navRows;

  // Whether navRows reflects a LOADED listing (not a transient empty while the
  // fetch is in flight). Only the non-search listing can be mid-load with rows
  // still empty AND a selection already set — that's the folder-open case: the
  // resolved Listing mounts with a selection restored from the pre-stat
  // provisional one, but its own /api/fs/list is briefly loading. Search keeps
  // its prior behavior (results stream in). Used by the reconcile effect so a
  // real, still-valid selection is never cleared as "vanished" during a reload.
  // "Settled" = not mid-fetch: an ok listing OR a terminal error (rows are
  // then genuinely empty, so the reconcile below should clear/reclamp a stale
  // selection). Only the transient `loading` status suppresses reconcile.
  const listingLoaded = searching ? true : state.status !== "loading";

  // Keep the keyboard selection scrolled into view as it moves. Follows the LEAD
  // row (`.lead`), not merely the first selected one: extending a Shift-range
  // downward must keep the moving end visible, and the top of the range is
  // usually the one that would otherwise win a `.selected` query.
  useEffect(() => {
    if (!selectedPath) return;
    (
      document.querySelector("table.listing-table tr.row.lead") ??
      document.querySelector("table.listing-table tr.row.selected")
    )?.scrollIntoView({ block: "nearest" });
  }, [selectedPath, navRows]);

  // Re-anchor the selection by PATH whenever the rows change (a refetch after
  // rename / duplicate / delete / move-to-bin) or the selection moves. Without
  // this the selected index kept pointing at the OLD name after a rename, so
  // pressing Enter opened a path that no longer existed.
  //   • A pending re-anchor (rename/duplicate target) is adopted the moment its
  //     row appears in the reloaded listing.
  //   • A still-present selection just refreshes its remembered slot index.
  //   • A vanished selection (deleted / trashed / moved) clamps to the nearest
  //     surviving row (or clears when the folder is now empty).
  // The pending wait is BOUNDED, not open-ended: it only holds while the current
  // selection is itself a live row. Renaming a search hit whose new path isn't a
  // search match leaves the pending target absent from navRows forever while the
  // old selected path also disappears — waiting unconditionally there would
  // strand the selection on a dead row (broken Enter). So once the old selection
  // is gone too, the pending target is abandoned and the normal clamp runs. The
  // pending path still lands the moment it does appear (e.g. search results
  // refetching to include the renamed file), so the happy path is unchanged.
  //   • Rows of a MULTI-selection that vanished are pruned while the lead
  //     survives, so a batch op that partly failed doesn't leave dead paths in
  //     the selection (and a later Cmd+C can't copy them).
  useEffect(() => {
    const rows = navRows;
    const pend = pendingSelectRef.current;
    let clampFallback = false;
    if (pend !== null) {
      const pi = rows.indexOf(pend);
      if (pi !== -1) {
        pendingSelectRef.current = null;
        lastSelIndexRef.current = pi;
        if (selectedPath !== pend || sel.paths.length !== 1) setSel(oneSelected(pend));
        return;
      }
      // Target not here yet. Keep waiting ONLY while the current selection is
      // still a real row (nothing's broken, the target may still arrive). If it
      // has also vanished, give up on the pending target and clamp below.
      if (selectedPath !== null && rows.indexOf(selectedPath) !== -1) return;
      pendingSelectRef.current = null;
      clampFallback = true;
    }
    if (selectedPath === null) {
      // No selection to reconcile. Only force one when a pending target was just
      // abandoned (so selection never stays dead); otherwise leave it unset.
      if (!clampFallback || rows.length === 0) return;
      const clamped = Math.min(Math.max(lastSelIndexRef.current, 0), rows.length - 1);
      setSel(oneSelected(rows[clamped]));
      return;
    }
    const i = rows.indexOf(selectedPath);
    if (i !== -1) {
      lastSelIndexRef.current = i; // lead still valid; remember its slot
      // Drop any other selected rows that are gone (deleted/moved/renamed).
      if (sel.paths.length > 1) {
        const live = new Set(rows);
        const kept = sel.paths.filter((p) => live.has(p));
        if (kept.length !== sel.paths.length) {
          setSel({
            paths: kept,
            anchor: sel.anchor !== null && live.has(sel.anchor) ? sel.anchor : selectedPath,
            lead: selectedPath,
          });
        }
      }
      return;
    }
    // Selection isn't in the current rows. While the listing is still LOADING
    // (rows transiently empty during a fetch — notably the pre-stat provisional
    // Listing being swapped for the resolved one right after a folder opens),
    // don't treat it as vanished: keep it and rerun once rows arrive. Clearing
    // here is what dropped an arrow-key selection made just after opening a
    // folder, even with the selection carried across the remount.
    if (!listingLoaded) return;
    if (rows.length === 0) {
      setSel(EMPTY_SELECTION);
      return;
    }
    const clamped = Math.min(Math.max(lastSelIndexRef.current, 0), rows.length - 1);
    setSel(oneSelected(rows[clamped]));
  }, [navRows, selectedPath, sel, listingLoaded]);

  // --- file operations ------------------------------------------------------

  // Which visible entries are cut sources — dimmed in the table. A cut can hold
  // several paths, so this is a set rather than one path.
  const cutSet = useMemo(
    () => new Set(clipboard?.op === "cut" ? clipboard.paths : []),
    [clipboard]
  );

  // The copy counterpart: marked with an accent edge + wash rather than dimmed
  // (a copy doesn't remove anything, so fading the source would lie). Exactly
  // one of cutSet/copiedSet is ever non-empty — the clipboard holds one op.
  const copiedSet = useMemo(
    () => new Set(clipboard?.op === "copy" ? clipboard.paths : []),
    [clipboard]
  );

  // Map every rendered row's path to its RowCtx, so a keyboard shortcut can
  // resolve the selected path back to a full row (is_dir etc.) the same way a
  // right-click does. Keyed off the arrays the table renders.
  const rowCtxByPath = useMemo(() => {
    const m = new Map<string, RowCtx>();
    if (searching) {
      for (const { entry } of visibleHits) {
        const path = base + "/" + entry.rel;
        m.set(path, {
          path,
          name: entry.rel.split("/").pop() ?? entry.rel,
          isDir: entry.is_dir,
          parentDir: dirname(path),
        });
      }
    } else {
      for (const entry of sortedEntries) {
        m.set(base + "/" + entry.name, {
          path: base + "/" + entry.name,
          name: entry.name,
          isDir: entry.is_dir,
          parentDir: base,
        });
      }
    }
    return m;
  }, [searching, visibleHits, sortedEntries, base]);
  rowCtxByPathRef.current = rowCtxByPath;

  // The selection as full rows, in rendered order (so a batch op processes rows
  // top-to-bottom regardless of the order they were clicked). Paths without a
  // rendered row — a search page not yet revealed, a row removed by a refetch
  // before the reconcile effect ran — are dropped: an op can only act on what
  // the user can actually see selected.
  const selectedRows = useMemo(() => {
    const chosen = new Set(sel.paths);
    return navRows.filter((p) => chosen.has(p)).map((p) => rowCtxByPath.get(p)!).filter(Boolean);
  }, [sel.paths, navRows, rowCtxByPath]);
  // The lead row, for the single-entry operations (Rename, paste target).
  const leadRow = sel.lead ? rowCtxByPath.get(sel.lead) : undefined;

  const refetch = () => setRefresh((n) => n + 1);

  // Run a mutating fs call, then refetch on success or surface its error as a
  // toast. The dir-watch socket also refetches, but that lags 300 ms and only
  // fires for the listed dir — an explicit refetch keeps the UI immediate.
  // `ctx` ({verb, name}) is optional but supplied by every menu action, so the
  // caught wire string is humanized (friendlyFsError) instead of leaking bare.
  const run = async (fn: () => Promise<unknown>, ctx?: { verb: string; name: string }) => {
    try {
      await fn();
      refetch();
    } catch (e) {
      pushToast({ msg: ctx ? friendlyFsError(e, ctx) : (e as Error).message, tone: "error" });
    }
  };

  // Belt-and-braces name guard for the New File / New Folder / Rename handlers:
  // the dialog already blocks invalid names, but re-check here (and toast) before
  // building a path so a "." / ".." / separator can never escape the folder.
  // Returns true when the name is rejected (caller should bail).
  const rejectName = (name: string): boolean => {
    const err = nameError(name);
    if (err) pushToast({ msg: err, tone: "error" });
    return err !== null;
  };

  // Guards a paste that's still running so a second Paste gesture (a rapid
  // Cmd+V×2) can't fire a parallel op on the same source — for a cut that
  // second call would renameEntry an already-moved src and 404 with a jarring
  // toast. Reset in the flight's .finally, so sequential copy-pastes stay fine.
  const pasteInFlight = useRef(false);

  // Paste into `dir`: a cut moves (rename) and clears the clipboard; a copy
  // duplicates and keeps it. Same basename in the target folder either way.
  // The TARGET is always a single folder; the SOURCE may be several paths (a
  // multi-row cut/copy), which are processed in order — sequentially, because
  // freePastePath resolves a name against a listing and parallel calls would
  // both pick the same free "… copy" name.
  // Reads the clipboard synchronously (getClipboard) and consumes a cut BEFORE
  // the await, so re-entry sees an empty clipboard and no-ops.
  const doPaste = (dir: string) => {
    const clip = getClipboard();
    if (!clip || clip.paths.length === 0 || pasteInFlight.current) return;
    const target = normDir(dir); // "" (root) → "/", and join avoids "//name"
    const { op } = clip;
    // A clipboard filled from search results can hold a folder AND entries
    // inside it (the hit list is a flat recursive walk). Paste the outermost
    // ancestors only: the folder's move/copy carries its contents, so a
    // descendant entry would either 404 on a source the parent already moved
    // (killing the rest of the batch) or, for a copy, drop a stray second copy
    // of the inner entry at the top of the target.
    const paths = pruneDescendantPaths(clip.paths);
    const label = paths.length === 1 ? basename(paths[0]) : `${paths.length} items`;
    if (op === "cut") setClipboard(null); // consume atomically, before any await
    pasteInFlight.current = true;
    run(async () => {
      const pasted: string[] = [];
      let last: string | null = null;
      try {
        for (const src of paths) {
          // Same-folder paste (dst would collide with the source), matching Finder:
          //   • CUT into its own folder is a no-op — the backend rename would 409
          //     on dst === src, so skip it (the clipboard is already cleared).
          //   • COPY into its own folder makes a deduped "… copy" instead of
          //     colliding (freeDuplicatePath, same as Duplicate).
          const sameFolder = join(target, basename(src)) === src;
          if (sameFolder && op === "cut") {
            pasted.push(src);
            continue;
          }
          // Both ops keep the name when free and dedupe to "… copy" when taken
          // (Finder keep-both), instead of surfacing a 409.
          const { is_dir } = await statPath(src);
          const dst = sameFolder
            ? await freeDuplicatePath(target, basename(src), is_dir)
            : await freePastePath(target, basename(src), is_dir);
          if (op === "cut") await renameEntry(src, dst);
          else await copyEntry(src, dst);
          pasted.push(src);
          last = dst;
        }
      } catch (e) {
        // The paste failed (e.g. a 403, or the source vanished); for a cut the
        // pre-clear above dropped the clipboard, so re-set the cut for whatever
        // hasn't moved yet and let run() toast the error — without this the user
        // would have to re-cut before retrying. Skip the restore if the user
        // cut/copied something newer mid-flight.
        // Restoring from the PRUNED list (not clip.paths) is what keeps the
        // retry viable: a descendant of an already-moved folder is gone from
        // its old location, so putting it back on the clipboard would make
        // every retry fail on the same dead source.
        if (op === "cut" && getClipboard() === null) {
          const left = paths.filter((p) => !pasted.includes(p));
          if (left.length) setClipboard({ paths: left, op: "cut" });
        }
        // A multi-path paste can move/copy some entries before throwing. run()
        // only refetches when the whole callback resolves, so refresh here or
        // the listing keeps showing rows that are already gone (or misses the
        // ones already written) until the 300 ms dir-watch catches up. The
        // rethrow is preserved so run() still toasts the failure.
        if (pasted.length) refetch();
        throw e;
      }
      // Re-anchor onto the last thing written, if it lands in this view.
      if (last !== null) pendingSelectRef.current = last;
    }, { verb: "paste", name: label }).finally(() => {
      pasteInFlight.current = false;
    });
  };

  // Duplicate into the same folder, picking the first free "… copy[/ n]" name
  // (freeDuplicatePath lists the folder so the copy never 409s on an existing
  // name).
  // In-flight guard, same idea as pasteInFlight: a rapid double Cmd+D would
  // race both calls to the same free "… copy" name and 409 the second.
  // Acts on the whole selection; the rows are duplicated one at a time for the
  // same reason paste is sequential (each freeDuplicatePath re-reads the folder,
  // so parallel calls would pick colliding names).
  const duplicateInFlight = useRef(false);
  const doDuplicate = (rows: RowCtx[]) => {
    if (!rows.length || duplicateInFlight.current) return;
    duplicateInFlight.current = true;
    run(async () => {
      let last: string | null = null;
      try {
        for (const row of rows) {
          const dst = await freeDuplicatePath(row.parentDir, row.name, row.isDir);
          await copyEntry(row.path, dst);
          last = dst;
        }
      } catch (e) {
        // Same partial-batch refresh as doPaste: run() refetches only on full
        // success, so copies already written would stay invisible here until
        // the dir-watch update. Rethrown so the error toast still shows.
        if (last !== null) refetch();
        throw e;
      }
      if (last !== null) pendingSelectRef.current = last; // select the new copy
    }, { verb: "duplicate", name: batchLabel(rows) }).finally(() => {
      duplicateInFlight.current = false;
    });
  };

  const doReveal = (path: string) => {
    revealPath(path).catch((e) =>
      pushToast({ msg: friendlyFsError(e, { verb: "reveal", name: basename(path) }), tone: "error" })
    );
  };

  const doCopyPath = (path: string) => {
    // Confirm with a non-error "info" toast; a failure (clipboard unavailable
    // or permission denied) stays silent — the path is still reachable via
    // Reveal in Finder.
    copyToClipboard(path).then((ok) => {
      if (ok) pushToast({ msg: "Path copied", tone: "info" });
    });
  };

  // Several paths go to the system clipboard newline-separated (what every file
  // manager writes for a multi-selection paste into a terminal or editor).
  const doCopyPaths = (paths: string[]) => {
    copyToClipboard(paths.join("\n")).then((ok) => {
      if (ok) pushToast({ msg: `${paths.length} paths copied`, tone: "info" });
    });
  };

  // Open Claude Code via its claude-cli:// scheme handler — a dir cwd's into
  // itself, a file cwd's into its parent and pre-fills an @-mention prompt.
  // Setting location.href to a custom scheme does not navigate the SPA away.
  const doOpenInClaude = (path: string, isDir: boolean, name: string, parentDir: string) => {
    window.location.href = claudeDeepLink(path, isDir, name, parentDir);
  };

  const startNewFile = (dir: string) =>
    setDialog({
      kind: "prompt",
      title: "New File",
      initial: "untitled.txt",
      confirmLabel: "Create",
      onConfirm: (name) => {
        if (rejectName(name)) return;
        // create=true: refuse (409 "conflict", surfaced as an error toast) if a
        // file with this name already exists, so New File never clobbers it.
        run(() => writeFile(join(normDir(dir), name), "", true), { verb: "create", name });
      },
    });

  const startNewFolder = (dir: string) =>
    setDialog({
      kind: "prompt",
      title: "New Folder",
      initial: "untitled folder",
      confirmLabel: "Create",
      onConfirm: (name) => {
        if (rejectName(name)) return;
        run(() => mkdir(join(normDir(dir), name)), { verb: "create", name });
      },
    });

  const startRename = (row: RowCtx) =>
    setDialog({
      kind: "prompt",
      title: "Rename",
      initial: row.name,
      confirmLabel: "Rename",
      selectStem: true,
      onConfirm: (name) => {
        if (name === row.name) return;
        if (rejectName(name)) return;
        const dst = join(normDir(row.parentDir), name);
        run(async () => {
          await renameEntry(row.path, dst);
          // Re-anchor onto the new name so the reloaded listing keeps this row
          // selected (and Enter opens the renamed file, not the dead old path).
          pendingSelectRef.current = dst;
          // The clipboard may still be pointing at the old path (or inside it,
          // if a renamed folder held the cut/copied entry) — repoint it so a
          // later Paste doesn't target a source that's now gone.
          remapClipboardPath(row.path, dst);
        }, { verb: "rename", name: row.name });
      },
    });

  // Hard delete, confirmed. Plural-aware: one row still names it (and says
  // whether it's a folder), several are counted.
  const startDelete = (allRows: RowCtx[]) => {
    // Drop rows contained by another selected folder before anything else, so
    // the confirm dialog counts what will actually be deleted and the loop below
    // never calls deleteEntry on a path the parent's recursive delete just took
    // (that 404 would abort the batch and toast a failure for a delete that in
    // fact removed everything asked for).
    const rows = pruneDescendantRows(allRows);
    if (!rows.length) return;
    const many = rows.length > 1;
    setDialog({
      kind: "confirm",
      title: many ? `Delete ${rows.length} items` : "Delete",
      message: many
        ? `Delete these ${rows.length} items? Any folders among them are deleted with everything inside. This can't be undone.`
        : rows[0].isDir
        ? `Delete the folder "${rows[0].name}" and everything inside it? This can't be undone.`
        : `Delete "${rows[0].name}"? This can't be undone.`,
      confirmLabel: many ? `Delete ${rows.length} items` : "Delete",
      danger: true,
      // recursive=true for a directory (its contents were named in the message).
      onConfirm: () =>
        run(async () => {
          let deleted = 0;
          try {
            for (const row of rows) {
              await deleteEntry(row.path, row.isDir);
              clearClipboardIfDeleted(row.path);
              deleted++;
            }
          } catch (e) {
            // Partial batch: run() refetches only on full success, so without
            // this the already-deleted rows linger in the listing until the
            // dir-watch update. Rethrown so run() still toasts the failure.
            if (deleted) refetch();
            throw e;
          }
        }, { verb: "delete", name: batchLabel(rows) }),
    });
  };

  // Move to Bin: a recoverable delete (macOS Trash), so no confirm dialog.
  // Acts on every row passed in (the whole selection). Where the server can't
  // trash (non-macOS → "unsupported") those rows fall back to the existing
  // confirm-then-hard-delete flow, which IS irreversible and so keeps its
  // warning. Success shows a low-key, count-aware info toast.
  const doTrash = (allRows: RowCtx[]) => {
    // As in startDelete: trashing a folder takes everything inside it, so a
    // selection that also holds rows from within that folder must not trash them
    // individually — the second call would hit a vanished path and be counted as
    // a real failure, replacing the "Moved to Bin" toast with a bogus error.
    const rows = pruneDescendantRows(allRows);
    if (!rows.length) return;
    void (async () => {
      const trashed: RowCtx[] = [];
      const unsupported: RowCtx[] = [];
      let failed: { row: RowCtx; message: string } | null = null;
      for (const row of rows) {
        const r = await trashEntry(row.path, row.isDir);
        if (r.status === "trashed") {
          trashed.push(row);
          clearClipboardIfDeleted(row.path);
        } else if (r.status === "unsupported") {
          unsupported.push(row);
        } else if (failed === null) {
          failed = { row, message: r.message };
        }
      }
      if (trashed.length) {
        pushToast({
          msg: trashed.length === 1 ? "Moved to Bin" : `Moved ${trashed.length} items to Bin`,
          tone: "info",
        });
        refetch();
      }
      // A real failure raises its own toast. It used to REPLACE the info one
      // above (one local slot, last write wins), which hid the fact that the
      // other rows did move; the shared stack shows both, which is what a
      // partial success actually is. The unsupported fallback only runs when
      // nothing errored.
      if (failed !== null) {
        pushToast({
          msg: friendlyFsError(failed.message, { verb: "move to Bin", name: failed.row.name }),
          tone: "error",
        });
      } else if (unsupported.length) {
        startDelete(unsupported);
      }
    })();
  };

  // Lazy loader for the Open With submenu: resolves the entry's template modes
  // (resolveOpenWithModes mirrors Preview's filter + condition-gate handling).
  // Selecting a mode navigates to the entry with `_mode` set; the default mode
  // deletes the param.
  const loadOpenWith = (path: string) => async (): Promise<MenuItem[]> => {
    const modes = await resolveOpenWithModes(path);
    return buildOpenWithItems(modes, (mode, isDefault) => {
      const search = isDefault ? "" : "?_mode=" + encodeURIComponent(mode);
      navigateUrl(urlForFsPath(path, search));
    });
  };

  // Menu for a right-clicked row (file or dir), in macOS Finder order. Paste
  // target follows Finder: into a dir, or the parent of a file. New File/Folder
  // live only on the background menu (Finder shows them there, not on a row).
  // `rows` is what the menu ACTS on: just the right-clicked row normally, or the
  // whole selection when the right-click landed inside a multi-row selection
  // (see openRowMenu). With several rows the entries that only make sense for
  // one — Open / Open With / Rename / Reveal / Open in Claude Code — are
  // dropped, and the batch entries count what they'll affect.
  const rowMenu = (row: RowCtx, rows: RowCtx[]): MenuEntry[] => {
    const dir = targetDirOf(row);
    const n = rows.length;
    if (n > 1) {
      return [
        { label: `Move ${n} items to Bin`, icon: MenuIcons.trash, onClick: () => doTrash(rows) },
        "separator",
        { label: `Duplicate ${n} items`, icon: MenuIcons.duplicate, onClick: () => doDuplicate(rows) },
        "separator",
        {
          label: `Cut ${n} items`,
          icon: MenuIcons.cut,
          onClick: () => setClipboard({ paths: rows.map((r) => r.path), op: "cut" }),
        },
        {
          label: `Copy ${n} items`,
          icon: MenuIcons.copy,
          onClick: () => setClipboard({ paths: rows.map((r) => r.path), op: "copy" }),
        },
        { label: "Paste", icon: MenuIcons.paste, disabled: !clipboard, onClick: () => doPaste(dir) },
        "separator",
        {
          label: `Copy ${n} Paths`,
          icon: MenuIcons.copyPath,
          onClick: () => doCopyPaths(rows.map((r) => r.path)),
        },
      ];
    }
    return [
      { label: isAppEntry(row.name, row.isDir) ? "Open App" : "Open", icon: MenuIcons.open, onClick: () => navigate(row.path, { isDir: row.isDir }) },
      { label: "Open With", icon: MenuIcons.openWith, submenu: loadOpenWith(row.path) },
      "separator",
      { label: "Move to Bin", icon: MenuIcons.trash, onClick: () => doTrash([row]) },
      "separator",
      { label: "Rename…", icon: MenuIcons.rename, onClick: () => startRename(row) },
      { label: "Duplicate", icon: MenuIcons.duplicate, onClick: () => doDuplicate([row]) },
      "separator",
      { label: "Cut", icon: MenuIcons.cut, onClick: () => setClipboard({ paths: [row.path], op: "cut" }) },
      { label: "Copy", icon: MenuIcons.copy, onClick: () => setClipboard({ paths: [row.path], op: "copy" }) },
      { label: "Paste", icon: MenuIcons.paste, disabled: !clipboard, onClick: () => doPaste(dir) },
      "separator",
      { label: "Copy Path", icon: MenuIcons.copyPath, onClick: () => doCopyPath(row.path) },
      { label: "Reveal in Finder", icon: MenuIcons.reveal, onClick: () => doReveal(row.path) },
      {
        label: "Open in Claude Code",
        icon: MenuIcons.openWith,
        onClick: () => doOpenInClaude(row.path, row.isDir, row.name, row.parentDir),
      },
    ];
  };

  // Menu for the empty listing background — operates on the current folder.
  // Finder order: New Folder before New File.
  const backgroundMenu = (): MenuEntry[] => [
    { label: "New Folder…", icon: MenuIcons.newFolder, onClick: () => startNewFolder(base) },
    { label: "New File…", icon: MenuIcons.newFile, onClick: () => startNewFile(base) },
    "separator",
    { label: "Paste", icon: MenuIcons.paste, disabled: !clipboard, onClick: () => doPaste(base) },
    "separator",
    { label: "Refresh", icon: MenuIcons.refresh, onClick: refetch },
    { label: "Reveal in Finder", icon: MenuIcons.reveal, onClick: () => doReveal(normDir(base)) },
    {
      label: "Open in Claude Code",
      icon: MenuIcons.openWith,
      onClick: () => doOpenInClaude(normDir(base), true, "", normDir(base)),
    },
  ];

  // Mouse selection on a row:
  //   • Shift+click  — select the contiguous range anchor..row (rendered order);
  //   • Mod+click    — toggle this row in/out and re-anchor on it;
  //   • plain click  — select only this row AND open it, which is what a single
  //     click has always done in this explorer (it's the primary way to browse,
  //     so multi-select doesn't take it away; the modified clicks are the ones
  //     that build a selection instead of navigating).
  // Native text selection is suppressed in onRowMouseDown, not here — see there.
  const onRowClick = (e: React.MouseEvent, path: string, row: RowCtx) => {
    if (e.shiftKey && !isMod(e)) {
      e.preventDefault();
      extendTo(path);
      return;
    }
    if (isMod(e)) {
      e.preventDefault();
      toggleSelected(path);
      return;
    }
    selectOnly(path);
    navigate(row.path, { isDir: row.isDir });
  };

  // Kill the browser's own text selection for Shift/Mod+click, on MOUSEDOWN —
  // the only moment early enough. `user-select: none` on tr.row (shell.css) is
  // necessary but NOT sufficient: it makes the row's own text unselectable, yet
  // a Shift+click still sets a selection ENDPOINT, so the browser happily paints
  // a range anchored at whatever selectable text was last clicked (a crumb, the
  // search box, anything outside the table) straight across the listing. So:
  // preventDefault stops a selection from being started or extended, and the
  // removeAllRanges collapses one that already existed before the gesture.
  // preventDefault on mousedown does not cancel the subsequent click, so
  // onRowClick still runs; rows aren't focusable, so the suppressed focus
  // side-effect costs nothing.
  const onRowMouseDown = (e: React.MouseEvent) => {
    if (!e.shiftKey && !isMod(e)) return;
    e.preventDefault();
    const sel = window.getSelection();
    if (sel && !sel.isCollapsed) sel.removeAllRanges();
  };

  // Right-clicking INSIDE an existing multi-row selection keeps it and acts on
  // the whole thing (Finder/Explorer behaviour); right-clicking anywhere else
  // collapses the selection onto that row first.
  const openRowMenu = (e: React.MouseEvent, row: RowCtx) => {
    e.preventDefault();
    e.stopPropagation(); // don't also open the background menu
    const inSelection = sel.paths.includes(row.path);
    const rows = inSelection && selectedRows.length > 1 ? selectedRows : [row];
    if (!inSelection) selectOnly(row.path);
    setMenu({ x: e.clientX, y: e.clientY, items: rowMenu(row, rows) });
  };

  // Fires only for the listing background (rows stopPropagation above).
  const openBackgroundMenu = (e: React.MouseEvent) => {
    e.preventDefault();
    setMenu({ x: e.clientX, y: e.clientY, items: backgroundMenu() });
  };

  // Keyboard shortcuts scoped to the listing: file operations on the selection
  // plus the folder-level navigation chords. Registered once (empty deps); the
  // handler is re-assigned each render so it always reads fresh state/closures.
  // Separate from the nav handler above, and non-overlapping with it by
  // construction: everything here carries the primary modifier or is a key that
  // handler ignores (F2, Delete, Backspace — its printable-key branch only fires
  // for single-character keys, and its arrow branch bails when isMod(e)).
  const shortcutRef = useRef<(e: KeyboardEvent) => void>(() => {});
  shortcutRef.current = (e: KeyboardEvent) => {
    if (e.isComposing) return;
    // Same hard guard as the nav handler: while a context menu or dialog is
    // open (in this view OR a hosting one, e.g. Preview's header menu with this
    // Listing embedded), file-op shortcuts (Cmd+Backspace trash, Cmd+X cut, …)
    // must not fire on the row behind it.
    if (overlayOpenRef.current || isOverlayOpen()) return;
    const el = document.activeElement as HTMLElement | null;
    const inSearch = el === searchInputRef.current;
    const navActive = inSearch || !el || el === document.body || el === document.documentElement;
    if (!navActive) return;
    // Every file operation acts on the WHOLE selection; the single-entry ones
    // (Rename, and the paste target) use the lead row.
    const rows = selectedRows;
    const row = leadRow;
    const mod = isMod(e);
    const key = e.key.toLowerCase();
    // The parent folder, for Mod+Up / bare Backspace. Equal to the current
    // folder at the filesystem (or drive) root, where there's nowhere to go.
    const here = normDir(base);
    const parent = dirname(here);
    const goParent = () => {
      if (parent !== here) navigate(parent, { isDir: true });
    };
    // With focus in the search box, Cmd+C/X/V must keep their native text
    // clipboard meaning — only the non-text shortcuts stay live there.
    if (inSearch && mod && (key === "c" || key === "x" || key === "v")) return;
    if (mod && key === "c") {
      if (!rows.length) return;
      e.preventDefault();
      setClipboard({ paths: rows.map((r) => r.path), op: "copy" });
    } else if (mod && key === "x") {
      if (!rows.length) return;
      e.preventDefault();
      setClipboard({ paths: rows.map((r) => r.path), op: "cut" });
    } else if (mod && key === "v") {
      if (!clipboard) return;
      e.preventDefault();
      // Paste is single-TARGET: into the lead row's folder (or itself, if it's a
      // directory), else the folder being listed.
      doPaste(row ? targetDirOf(row) : base);
    } else if (mod && key === "d") {
      if (!rows.length) return;
      e.preventDefault();
      doDuplicate(rows);
    } else if (mod && e.key === "ArrowDown") {
      // Open the lead row — the same gesture as Enter (macOS Cmd+Down).
      if (!row) return;
      e.preventDefault();
      navigate(row.path, { isDir: row.isDir });
    } else if (mod && e.key === "ArrowUp") {
      e.preventDefault();
      goParent();
    } else if (mod && (e.key === "[" || e.key === "]")) {
      // Back / forward. The router only ever pushes, so this drives the browser
      // history directly — popstate is what the shell listens to anyway
      // (useNavEpoch), so the view remounts exactly as it does for a Back click.
      e.preventDefault();
      if (e.key === "[") history.back();
      else history.forward();
    } else if (mod && e.shiftKey && key === "n") {
      e.preventDefault();
      startNewFolder(base);
    } else if (mod && key === "r") {
      // The app's own listing refresh, not a page reload — preventDefault is
      // what stops the browser from throwing the whole SPA away.
      e.preventDefault();
      refetch();
    } else if (mod && e.key === "Backspace") {
      // macOS trash chord. Windows/Linux use the Delete key below instead.
      // Never while typing in the search box: Cmd+Delete is the standard macOS
      // "clear to start of line" chord, so trashing there would be a foot-gun.
      if (!isMac || inSearch || !rows.length) return;
      e.preventDefault();
      doTrash(rows);
      // Bare key only: because isMod() is exclusive, `!mod` alone is still true
      // when the OTHER modifier is held (Super+Backspace on Linux), so test the
      // raw flags instead.
    } else if (e.key === "Backspace" && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey) {
      // Windows/Linux: bare Backspace goes up a folder. On macOS it must stay
      // inert — Cmd+Backspace is trash there and a bare Backspace navigating
      // away would be a foot-gun. Never while typing in the search box.
      if (isMac || inSearch) return;
      e.preventDefault();
      goParent();
    } else if (e.key === "Delete" && !e.metaKey && !e.ctrlKey) {
      // Windows/Linux trash key. On macOS the Delete (⌦) key is not the trash
      // gesture — Cmd+Backspace above is. Raw-flag test rather than `!mod` so a
      // held Super key can't slip a destructive key through (see Backspace).
      if (isMac || inSearch || !rows.length) return;
      e.preventDefault();
      doTrash(rows);
    } else if (e.key === "F2") {
      // Rename is single-entry: with several rows selected it renames the LEAD
      // row (what Windows Explorer does — F2 edits the focused item), not a
      // no-op and never a batch rename.
      if (!row) return;
      e.preventDefault();
      startRename(row);
    }
  };
  useEffect(() => {
    const h = (e: KeyboardEvent) => shortcutRef.current(e);
    document.addEventListener("keydown", h);
    return () => document.removeEventListener("keydown", h);
  }, []);

  // --- table body -----------------------------------------------------------

  let body: React.ReactNode;
  if (searching) {
    if (validWalk.status === "error") {
      body = (
        <tr>
          <td colSpan={3} className="status-message error">
            Search failed: {validWalk.message}
          </td>
        </tr>
      );
    } else if (validWalk.status === "ok" || validWalk.status === "streaming") {
      if (hits.length) {
        body = (
          <>
            {visibleHits.map(({ entry, positions }) => {
              const childPath = base + "/" + entry.rel;
              return (
                <tr
                  key={entry.rel}
                  className={
                    "row" +
                    (selectedSet.has(childPath) ? " selected" : "") +
                    // Marker only (no styling of its own): the lead row is what
                    // the scroll-into-view effect tracks.
                    (childPath === selectedPath ? " lead" : "") +
                    (cutSet.has(childPath) ? " cut" : "") +
                    (copiedSet.has(childPath) ? " copied" : "")
                  }
                  onClick={(e) =>
                    onRowClick(e, childPath, {
                      path: childPath,
                      name: entry.rel.split("/").pop() ?? entry.rel,
                      isDir: entry.is_dir,
                      parentDir: dirname(childPath),
                    })
                  }
                  onMouseDown={onRowMouseDown}
                  onContextMenu={(e) =>
                    openRowMenu(e, {
                      path: childPath,
                      name: entry.rel.split("/").pop() ?? entry.rel,
                      isDir: entry.is_dir,
                      parentDir: dirname(childPath),
                    })
                  }
                >
                  <td className="name">
                    <span className="icon">{iconForEntry(entry.rel.split("/").pop() ?? entry.rel, entry.is_dir)}</span>
                    <span className="search-path">{renderHighlight(entry.rel, positions)}</span>
                  </td>
                  <td className="size">{entry.is_dir ? "" : formatSize(entry.size)}</td>
                  <td className="mtime">{formatMtime(entry.mtime)}</td>
                </tr>
              );
            })}
            {hasMore && (
              <tr ref={sentinelRef}>
                <td colSpan={3} className="status-message">
                  Scroll for more…
                </td>
              </tr>
            )}
          </>
        );
      } else {
        // No matches. Say so honestly: distinguish "still looking" (stream
        // running) and "the walk didn't even cover everything" (truncated) —
        // the old UI showed a bare "No matches" even when the file existed
        // in a region the capped walk never reached.
        const message =
          validWalk.status === "streaming"
            ? `No matches yet — still searching (${validWalk.count.toLocaleString()} entries scanned)`
            : validWalk.truncated
            ? `No matches in the first ${validWalk.total.toLocaleString()} entries — this folder tree is too large to search fully`
            : "No matches";
        body = (
          <tr>
            <td colSpan={3} className="status-message">
              {message}
            </td>
          </tr>
        );
      }
    } else {
      body = (
        <tr>
          <td colSpan={3} className="status-message">
            Searching…
          </td>
        </tr>
      );
    }
  } else if (state.status === "loading") {
    body = skeletonRows(8);
  } else if (state.status === "error") {
    // In the provisional scaffold phase a list failure is most likely a stale
    // dir hint pointing at a file (its /api/fs/list 404s); suppress the hard
    // error and show the neutral loading skeleton — stat is still resolving and
    // will replace this scaffold with the correct file view. Post-stat
    // (committed render), a genuine list failure surfaces normally.
    body = provisional ? (
      skeletonRows(8)
    ) : (
      <tr>
        <td colSpan={3} className="status-message error">
          Failed to list {fsPath}: {state.message}
        </td>
      </tr>
    );
  } else {
    const rows = sortedEntries.map((entry) => {
      const childPath = base + "/" + entry.name;
      return (
        <tr
          key={entry.name}
          className={
            (entry.ignored ? "row ignored" : "row") +
            (selectedSet.has(childPath) ? " selected" : "") +
            (childPath === selectedPath ? " lead" : "") + // scroll-into-view marker
            (cutSet.has(childPath) ? " cut" : "") +
            (copiedSet.has(childPath) ? " copied" : "")
          }
          onClick={(e) =>
            onRowClick(e, childPath, {
              path: childPath,
              name: entry.name,
              isDir: entry.is_dir,
              parentDir: base,
            })
          }
          onMouseDown={onRowMouseDown}
          onContextMenu={(e) =>
            openRowMenu(e, {
              path: childPath,
              name: entry.name,
              isDir: entry.is_dir,
              parentDir: base,
            })
          }
        >
          <td className="name">
            <span className="icon">{iconForEntry(entry.name, entry.is_dir)}</span>
            {entry.name}
          </td>
          <td className="size">{entry.is_dir ? "" : formatSize(entry.size)}</td>
          <td className="mtime">{formatMtime(entry.mtime)}</td>
        </tr>
      );
    });
    // A truncated listing gets a slim banner row after the entries: the
    // directory has more than the server cap. On the resumable S3-direct route
    // (cursor non-null) it carries a Load more button that appends the next
    // page; otherwise it just states the listing is partial.
    const banner = state.truncated ? (
      <tr key="__truncated__" className="listing-truncated">
        <td colSpan={3} className="status-message">
          Showing first {sortedEntries.length} entries — directory listing is partial.
          {state.cursor && (
            <button
              type="button"
              className="listing-load-more"
              disabled={loadingMore}
              onClick={loadMore}
            >
              {loadingMore ? "Loading…" : "Load more"}
            </button>
          )}
        </td>
      </tr>
    ) : null;
    body =
      rows.length || banner ? (
        <>
          {rows}
          {banner}
        </>
      ) : (
        <tr>
          <td colSpan={3} className="status-message">
            Empty directory
          </td>
        </tr>
      );
  }

  // --- search match count (inline in the search row) ------------------------

  let searchCount: string | null = null;
  let searchCountTitle: string | undefined;
  if (searching && validWalk.status === "streaming") {
    // Live progress while the walk streams: match count so far + how much of
    // the tree has been scanned. Updates in place, no layout shift.
    searchCount = `${hits.length.toLocaleString()} match${hits.length === 1 ? "" : "es"} · ${validWalk.count.toLocaleString()} scanned…`;
  } else if (searching && validWalk.status === "ok" && hits.length > 0) {
    // A truncated walk (server safety cap) means `hits` undercounts the real
    // tree. Signal that without new UI: a "+" on the number plus a tooltip.
    const suffix = validWalk.truncated ? "+" : "";
    searchCount = `${hits.length.toLocaleString()}${suffix} match${hits.length === 1 ? "" : "es"}`;
    if (validWalk.truncated)
      searchCountTitle = `Search covers the first ${validWalk.total.toLocaleString()} entries of this folder tree`;
  }

  return (
    <div className="listing">
      <div className="listing-search">
        {/* The box wraps input + pinned chips so the pane toggle can sit to
            their right without disturbing the chips' inside-the-input pin. */}
        <div className="listing-search-box">
          <input
            ref={searchInputRef}
            type="search"
            className="listing-search-input"
            placeholder="Start typing to search…"
            value={query}
            onFocus={prefetchWalk}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                e.preventDefault();
                setQuery("");
                e.currentTarget.blur();
              }
            }}
          />
          {searching && (validWalk.status === "idle" || validWalk.status === "streaming") && (
            <span className="listing-search-spinner" aria-hidden="true" />
          )}
          {searchCount !== null && (
            <span className="listing-search-count" title={searchCountTitle}>
              {searchCount}
            </span>
          )}
          {/* Multi-selection readout — a single selected row needs no count. */}
          {sel.paths.length > 1 && (
            <span className="listing-search-count">{sel.paths.length} selected</span>
          )}
        </div>
        <button
          type="button"
          className={"listing-pane-toggle" + (pane.on ? " active" : "")}
          title={pane.on ? "Hide preview pane" : "Show preview pane"}
          aria-pressed={pane.on}
          onClick={togglePane}
        >
          <SplitRightIcon />
        </button>
      </div>
      <div className="listing-split" ref={splitRef}>
        <div
          className={"listing-scroll" + (isStale ? " listing-stale" : "")}
          onContextMenu={openBackgroundMenu}
        >
          <table className="listing-table">
            <thead>
              <tr>
                {(Object.entries(SORT_KEYS) as [SortKey, string][]).map(([key, label]) =>
                  searching ? (
                    // While searching, headers sort the results; no active arrow
                    // means relevance (fuzzy-rank) order.
                    <th
                      key={key}
                      className={"sortable" + (searchSort?.sort === key ? " sorted" : "")}
                      onClick={() => setSearchSortKey(key)}
                    >
                      {label}
                      {searchSort?.sort === key && (
                        <span className="sort-arrow">{searchSort.order === "asc" ? "▲" : "▼"}</span>
                      )}
                    </th>
                  ) : (
                    <th
                      key={key}
                      className={"sortable" + (key === sort ? " sorted" : "")}
                      onClick={() => setSort(key)}
                    >
                      {label}
                      {key === sort && <span className="sort-arrow">{order === "asc" ? "▲" : "▼"}</span>}
                    </th>
                  )
                )}
              </tr>
            </thead>
            <tbody>{body}</tbody>
          </table>
        </div>
        {pane.on && (
          <>
            <div
              className="listing-divider"
              onPointerDown={onDividerPointerDown}
              role="separator"
              aria-orientation="vertical"
            />
            <div className="listing-pane-slot" style={{ flexBasis: pane.width }}>
              {/* Keyed on the previewed path: switching rows remounts the pane,
                  so a stale iframe never lingers a frame while the new row's
                  stat/list resolves. */}
              <ListingPreviewPane
                key={sel.paths.length === 1 && leadRow ? leadRow.path : "none"}
                row={sel.paths.length === 1 && leadRow ? leadRow : null}
                selCount={sel.paths.length}
              />
            </div>
          </>
        )}
      </div>

      {menu && (
        <ContextMenu x={menu.x} y={menu.y} items={menu.items} onClose={() => setMenu(null)} />
      )}

      {dialog?.kind === "prompt" && (
        <PromptDialog
          title={dialog.title}
          initialValue={dialog.initial}
          confirmLabel={dialog.confirmLabel}
          selectStem={dialog.selectStem}
          onConfirm={(v) => {
            const { onConfirm } = dialog;
            setDialog(null);
            onConfirm(v);
          }}
          onCancel={() => setDialog(null)}
        />
      )}
      {dialog?.kind === "confirm" && (
        <ConfirmDialog
          title={dialog.title}
          message={dialog.message}
          confirmLabel={dialog.confirmLabel}
          danger={dialog.danger}
          onConfirm={() => {
            const { onConfirm } = dialog;
            setDialog(null);
            onConfirm();
          }}
          onCancel={() => setDialog(null)}
        />
      )}

    </div>
  );
}
