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
import { useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { navigate, navigateUrl, urlForFsPath } from "../lib/router";
import {
  listDir,
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
  copyToClipboard,
  clearClipboardIfDeleted,
  trashEntry,
  resolveOpenWithModes,
  buildOpenWithItems,
} from "../lib/fs-actions";
import { acquireOverlay, releaseOverlay, isOverlayOpen } from "../lib/ui-overlay";
import { formatSize, formatMtime, basename } from "../lib/format";
import { fuzzyMatch, highlightSegments } from "../lib/fuzzy";
import { iconForEntry } from "../components/FileIcons";
import { getViewState, setViewState } from "../lib/viewstate";
import { getClipboard, setClipboard, useClipboard } from "../lib/fs-clipboard";
import ContextMenu, { type MenuEntry, type MenuItem } from "../components/ContextMenu";
import { MenuIcons } from "../components/MenuIcons";
import { PromptDialog, ConfirmDialog, nameError } from "../components/FsDialogs";
import Toast from "../components/Toast";

// A right-clicked row, normalized so both listing rows (name relative to the
// listed folder) and search-result rows (a `rel` path into a subtree) drive the
// same menu. `parentDir` is the containing folder; `path` is the entry itself.
interface RowCtx {
  path: string;
  name: string;
  isDir: boolean;
  parentDir: string;
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

function currentQuery(): string {
  return new URLSearchParams(location.search).get("q") || "";
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
    if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1; // dirs group first within each
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
  | { status: "ok"; entries: FsEntry[] }
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

export default function Listing({ fsPath }: { fsPath: string }) {
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
    const saved = getViewState(fsPath);
    if (!saved) return; // nothing stored → leave default sort + clean URL
    const s = new URLSearchParams(saved);
    const params = new URLSearchParams(location.search);
    params.set("sort", s.get("sort") || "name");
    params.set("order", s.get("order") === "desc" ? "desc" : "asc");
    history.replaceState(null, "", location.pathname + "?" + params.toString());
  }, [fsPath]);
  const [refresh, setRefresh] = useState(0); // bumped by the dir watch socket
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
  // Path of the keyboard-selected row (arrow-key navigation); null = none.
  const [selectedPath, setSelectedPath] = useState<string | null>(null);

  // --- Context-menu / file-operation state ----------------------------------
  // The open context menu (position + items), the open modal, and a transient
  // toast (error, or a non-red "info" confirmation). All local to this folder
  // view. The cut/copy clipboard is a module-level store (lib/fs-clipboard) so
  // it survives this component's per-folder remount (see there).
  const clipboard = useClipboard();
  const [menu, setMenu] = useState<{ x: number; y: number; items: MenuEntry[] } | null>(null);
  const [dialog, setDialog] = useState<DialogState | null>(null);
  const [toast, setToast] = useState<{ msg: string; tone: "error" | "info" } | null>(null);

  // Auto-dismiss the toast so it doesn't linger; a new toast resets it.
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 6000);
    return () => clearTimeout(t);
  }, [toast]);

  // Search input, so a keystroke anywhere in the listing can focus it.
  const searchInputRef = useRef<HTMLInputElement>(null);
  // Latest ordered list of navigable row paths + the current selection, read by
  // the document keydown handler (registered once, so it can't close over them).
  const navRowsRef = useRef<string[]>([]);
  const selectedPathRef = useRef<string | null>(null);
  selectedPathRef.current = selectedPath;
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
  useEffect(() => {
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

  // Keyboard navigation for the listing, whether focus is in the search box or
  // nowhere in particular:
  //   • a plain printable key focuses the search box so the character lands there;
  //   • Up/Down move the selection through the rendered rows — in the search box
  //     too, since a single-line input doesn't need them for the caret;
  //   • Enter opens the selection, or the top row when nothing is selected yet.
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

      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        if (!navActive) return;
        const rows = navRowsRef.current;
        if (!rows.length) return;
        e.preventDefault();
        const idx = rows.indexOf(selectedPathRef.current ?? "");
        // Nothing selected yet: Down starts at the top, Up at the bottom.
        let next =
          idx === -1
            ? e.key === "ArrowDown" ? 0 : rows.length - 1
            : e.key === "ArrowDown" ? idx + 1 : idx - 1;
        next = Math.max(0, Math.min(rows.length - 1, next));
        setSelectedPath(rows[next]);
        return;
      }
      if (e.key === "Enter") {
        if (!navActive) return;
        const rows = navRowsRef.current;
        if (!rows.length) return;
        const idx = rows.indexOf(selectedPathRef.current ?? "");
        e.preventDefault();
        navigate(idx === -1 ? rows[0] : rows[idx]);
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
    listDir(fsPath).then(
      (data) => alive && setState({ status: "ok", entries: data.entries }),
      (err: Error) => alive && setState({ status: "error", message: err.message })
    );
    return () => {
      alive = false;
    };
  }, [fsPath, refresh]);

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
      history.replaceState(null, "", location.pathname + (qs ? "?" + qs : ""));
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
    history.replaceState(null, "", location.pathname + "?" + params.toString());
    setSortState(next);
    // Remember this folder's choice so returning to it later restores this sort.
    // Only sort/order are persisted — the in-folder search `q` stays transient.
    setViewState(fsPath, "?sort=" + next.sort + "&order=" + next.order);
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

  // Keep the keyboard selection scrolled into view as it moves.
  useEffect(() => {
    if (!selectedPath) return;
    document
      .querySelector("table.listing-table tr.row.selected")
      ?.scrollIntoView({ block: "nearest" });
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
  useEffect(() => {
    const rows = navRows;
    const pend = pendingSelectRef.current;
    let clampFallback = false;
    if (pend !== null) {
      const pi = rows.indexOf(pend);
      if (pi !== -1) {
        pendingSelectRef.current = null;
        lastSelIndexRef.current = pi;
        if (selectedPath !== pend) setSelectedPath(pend);
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
      setSelectedPath(rows[clamped]);
      return;
    }
    const i = rows.indexOf(selectedPath);
    if (i !== -1) {
      lastSelIndexRef.current = i; // selection still valid; remember its slot
      return;
    }
    if (rows.length === 0) {
      setSelectedPath(null);
      return;
    }
    const clamped = Math.min(Math.max(lastSelIndexRef.current, 0), rows.length - 1);
    setSelectedPath(rows[clamped]);
  }, [navRows, selectedPath]);

  // --- file operations ------------------------------------------------------

  // Which visible entry (if any) is the cut source — dimmed in the table.
  const cutPath = clipboard?.op === "cut" ? clipboard.path : null;

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

  const refetch = () => setRefresh((n) => n + 1);

  // Run a mutating fs call, then refetch on success or surface its error as a
  // toast. The dir-watch socket also refetches, but that lags 300 ms and only
  // fires for the listed dir — an explicit refetch keeps the UI immediate.
  const run = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      refetch();
    } catch (e) {
      setToast({ msg: (e as Error).message, tone: "error" });
    }
  };

  // Belt-and-braces name guard for the New File / New Folder / Rename handlers:
  // the dialog already blocks invalid names, but re-check here (and toast) before
  // building a path so a "." / ".." / separator can never escape the folder.
  // Returns true when the name is rejected (caller should bail).
  const rejectName = (name: string): boolean => {
    const err = nameError(name);
    if (err) setToast({ msg: err, tone: "error" });
    return err !== null;
  };

  // Guards a paste that's still running so a second Paste gesture (a rapid
  // Cmd+V×2) can't fire a parallel op on the same source — for a cut that
  // second call would renameEntry an already-moved src and 404 with a jarring
  // toast. Reset in the flight's .finally, so sequential copy-pastes stay fine.
  const pasteInFlight = useRef(false);

  // Paste into `dir`: a cut moves (rename) and clears the clipboard; a copy
  // duplicates and keeps it. Same basename in the target folder either way.
  // Reads the clipboard synchronously (getClipboard) and consumes a cut BEFORE
  // the await, so re-entry sees an empty clipboard and no-ops.
  const doPaste = (dir: string) => {
    const clip = getClipboard();
    if (!clip || pasteInFlight.current) return;
    const target = normDir(dir); // "" (root) → "/", and join avoids "//name"
    const { path: src, op } = clip;
    const dst = join(target, basename(src));
    // Same-folder paste (dst would collide with the source), matching Finder:
    //   • CUT into its own folder is a no-op — the backend rename would 409 on
    //     dst === src, so just drop the cut clipboard (nothing to move).
    //   • COPY into its own folder makes a deduped "… copy" instead of colliding
    //     (freeDuplicatePath, same as Duplicate), and re-anchors onto the copy.
    if (dst === src) {
      if (op === "cut") {
        setClipboard(null);
        return;
      }
      pasteInFlight.current = true;
      run(async () => {
        const { is_dir } = await statPath(src);
        const copyDst = await freeDuplicatePath(target, basename(src), is_dir);
        await copyEntry(src, copyDst);
        pendingSelectRef.current = copyDst; // move selection onto the new copy
      }).finally(() => {
        pasteInFlight.current = false;
      });
      return;
    }
    if (op === "cut") setClipboard(null); // consume atomically, before any await
    pasteInFlight.current = true;
    run(async () => {
      if (op === "cut") {
        try {
          await renameEntry(src, dst);
        } catch (e) {
          // The move was rejected (e.g. a 409/403); the pre-clear above dropped
          // the clipboard, so re-set it to the same cut and let run() toast the
          // error. Without this the user would have to re-cut before retrying.
          setClipboard({ path: src, op: "cut" });
          throw e;
        }
      } else {
        await copyEntry(src, dst);
      }
    }).finally(() => {
      pasteInFlight.current = false;
    });
  };

  // Duplicate into the same folder, picking the first free "… copy[/ n]" name
  // (freeDuplicatePath lists the folder so the copy never 409s on an existing
  // name).
  const doDuplicate = (row: RowCtx) => {
    run(async () => {
      const dst = await freeDuplicatePath(row.parentDir, row.name, row.isDir);
      await copyEntry(row.path, dst);
      pendingSelectRef.current = dst; // move selection onto the new copy
    });
  };

  const doReveal = (path: string) => {
    revealPath(path).catch((e) => setToast({ msg: (e as Error).message, tone: "error" }));
  };

  const doCopyPath = (path: string) => {
    // Confirm with a non-error "info" toast; a failure (clipboard unavailable
    // or permission denied) stays silent — the path is still reachable via
    // Reveal in Finder.
    copyToClipboard(path).then((ok) => {
      if (ok) setToast({ msg: "Path copied", tone: "info" });
    });
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
        run(() => writeFile(join(normDir(dir), name), "", true));
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
        run(() => mkdir(join(normDir(dir), name)));
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
        });
      },
    });

  const startDelete = (row: RowCtx) =>
    setDialog({
      kind: "confirm",
      title: "Delete",
      message: row.isDir
        ? `Delete the folder "${row.name}" and everything inside it? This can't be undone.`
        : `Delete "${row.name}"? This can't be undone.`,
      confirmLabel: "Delete",
      danger: true,
      // recursive=true for a directory (its contents were named in the message).
      onConfirm: () =>
        run(async () => {
          await deleteEntry(row.path, row.isDir);
          clearClipboardIfDeleted(row.path);
        }),
    });

  // Move to Bin: a recoverable delete (macOS Trash), so no confirm dialog.
  // Where the server can't trash (non-macOS → "unsupported") this falls back to
  // the existing confirm-then-hard-delete flow, which IS irreversible and so
  // keeps its warning. Success shows a low-key info toast.
  const doTrash = (row: RowCtx) => {
    trashEntry(row.path, row.isDir).then((r) => {
      if (r.status === "trashed") {
        setToast({ msg: "Moved to Bin", tone: "info" });
        clearClipboardIfDeleted(row.path);
        refetch();
      } else if (r.status === "unsupported") {
        startDelete(row);
      } else {
        setToast({ msg: r.message, tone: "error" });
      }
    });
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
  const rowMenu = (row: RowCtx): MenuEntry[] => {
    const dir = targetDirOf(row);
    return [
      { label: "Open", icon: MenuIcons.open, onClick: () => navigate(row.path) },
      { label: "Open With", icon: MenuIcons.openWith, submenu: loadOpenWith(row.path) },
      "separator",
      { label: "Move to Bin", icon: MenuIcons.trash, onClick: () => doTrash(row) },
      "separator",
      { label: "Rename…", icon: MenuIcons.rename, onClick: () => startRename(row) },
      { label: "Duplicate", icon: MenuIcons.duplicate, onClick: () => doDuplicate(row) },
      "separator",
      { label: "Cut", icon: MenuIcons.cut, onClick: () => setClipboard({ path: row.path, op: "cut" }) },
      { label: "Copy", icon: MenuIcons.copy, onClick: () => setClipboard({ path: row.path, op: "copy" }) },
      { label: "Paste", icon: MenuIcons.paste, disabled: !clipboard, onClick: () => doPaste(dir) },
      "separator",
      { label: "Copy Path", icon: MenuIcons.copyPath, onClick: () => doCopyPath(row.path) },
      { label: "Reveal in Finder", icon: MenuIcons.reveal, onClick: () => doReveal(row.path) },
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
    { label: "Reveal in Finder", icon: MenuIcons.reveal, onClick: () => doReveal(base) },
  ];

  const openRowMenu = (e: React.MouseEvent, row: RowCtx) => {
    e.preventDefault();
    e.stopPropagation(); // don't also open the background menu
    setSelectedPath(row.path);
    setMenu({ x: e.clientX, y: e.clientY, items: rowMenu(row) });
  };

  // Fires only for the listing background (rows stopPropagation above).
  const openBackgroundMenu = (e: React.MouseEvent) => {
    e.preventDefault();
    setMenu({ x: e.clientX, y: e.clientY, items: backgroundMenu() });
  };

  // Keyboard shortcuts scoped to the listing, active only with a row selected.
  // Registered once (empty deps); the handler is re-assigned each render so it
  // always reads fresh state/closures. Separate from the nav handler above —
  // these carry a modifier (or F2), which that handler explicitly ignores, so
  // there's no clash.
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
    const sel = selectedPathRef.current;
    const row = sel ? rowCtxByPath.get(sel) : undefined;
    const mod = e.metaKey || e.ctrlKey;
    const key = e.key.toLowerCase();
    if (mod && key === "c") {
      if (!row) return;
      e.preventDefault();
      setClipboard({ path: row.path, op: "copy" });
    } else if (mod && key === "x") {
      if (!row) return;
      e.preventDefault();
      setClipboard({ path: row.path, op: "cut" });
    } else if (mod && key === "v") {
      if (!clipboard) return;
      e.preventDefault();
      doPaste(row ? targetDirOf(row) : base);
    } else if (mod && key === "d") {
      if (!row) return;
      e.preventDefault();
      doDuplicate(row);
    } else if (mod && e.key === "Backspace") {
      if (!row) return;
      e.preventDefault();
      doTrash(row);
    } else if (e.key === "F2") {
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
                    (childPath === selectedPath ? " selected" : "") +
                    (childPath === cutPath ? " cut" : "")
                  }
                  onClick={() => navigate(childPath)}
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
    body = (
      <tr>
        <td colSpan={3} className="status-message">
          Loading…
        </td>
      </tr>
    );
  } else if (state.status === "error") {
    body = (
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
            (childPath === selectedPath ? " selected" : "") +
            (childPath === cutPath ? " cut" : "")
          }
          onClick={() => navigate(childPath)}
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
    body = rows.length ? (
      rows
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
      </div>
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

      {toast && <Toast msg={toast.msg} tone={toast.tone} onClose={() => setToast(null)} />}
    </div>
  );
}
