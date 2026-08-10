// Directory listing view with sortable columns and an in-folder search.
// Sort state lives in the URL (?sort=name|size|mtime&order=asc|desc) so a
// sorted listing is refresh-proof and bookmarkable like any other view state;
// the search query rides the URL the same way (?q=…). A non-empty query swaps
// the listing for flat, rank-ordered results over a recursive walk of the
// folder — see listing/useWalkSearch for the streaming/scoring pipeline.
//
// This file is the orchestrator: it wires the hooks together and renders the
// table. The pieces live in listing/:
//   types.ts               shared types + tuning constants
//   sorting.ts             sort resolution + entry sorting
//   search.ts              fuzzy scoring / ranking (pure)
//   selection.ts           selection model + cross-remount stash (pure)
//   pane.ts                preview-pane split (usePreviewPane: width + drag)
//   row-utils.ts           RowCtx batch helpers
//   bits.tsx               skeleton rows, ClipMark, highlight, scroll anchor
//   useDirListing.ts       /api/fs/list fetch, Load more, dir watch, new-row cue
//   useWalkSearch.ts       streamed walk + scoring + throttles + result paging
//   useListingSelection.ts selection state + keyboard nav + reconcile
//   useFileOps.ts          file operations + context menus + dialogs
//   shortcut-chord.ts       which chord means which action (pure)
//   useListingShortcuts.ts file-op keyboard chords
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { navigate, replaceSearch } from "@platform/lib/router";
import { dirname, normDir } from "@apps/explorer/lib/fs-actions";
import { acquireOverlay, releaseOverlay } from "@platform/lib/ui-overlay";
import { isMod } from "@platform/lib/platform";
import { formatSize, formatMtime, formatMtimeFull } from "@platform/lib/format";
import { iconForEntry, isAppEntry } from "@platform/ui/FileIcons";
import { getViewState, setViewState } from "@platform/lib/viewstate";
import { useFlip, FLIP_KEY_ATTR } from "@platform/lib/flip";
import { useClipboard } from "@apps/explorer/lib/fs-clipboard";
import ContextMenu from "@platform/ui/ContextMenu";
import { PromptDialog, ConfirmDialog } from "@apps/explorer/FsDialogs";
import ListingPreviewPane from "@apps/explorer/ListingPreviewPane";
import {
  FLIP_MAX_ROWS,
  SORT_KEYS,
  type RowCtx,
  type SortKey,
  type SortOrder,
} from "@apps/explorer/listing/types";
import { resolveSort, sortEntries } from "@apps/explorer/listing/sorting";
import {
  skeletonRows,
  ClipMark,
  renderHighlight,
  measureScrollAnchor,
} from "@apps/explorer/listing/bits";
import { usePreviewPane } from "@apps/explorer/listing/pane";
import { autoSelectPath, selectionClaimed } from "@apps/explorer/listing/selection";
import { useDirListing } from "@apps/explorer/listing/useDirListing";
import { useWalkSearch } from "@apps/explorer/listing/useWalkSearch";
import { useListingSelection } from "@apps/explorer/listing/useListingSelection";
import { useFileOps } from "@apps/explorer/listing/useFileOps";
import { useListingShortcuts } from "@apps/explorer/listing/useListingShortcuts";

export default function Listing({
  fsPath,
  provisional = false,
  embedded = false,
  onSingleApp,
}: {
  fsPath: string;
  // `provisional`: this Listing is rendering inside the pre-stat loading
  // scaffold (App LoadingScaffold), mounted off a directory NAV HINT rather
  // than a confirmed stat. The hint is authoritative in practice but can be
  // stale — if the path is actually a file, /api/fs/list 404s. In that
  // provisional phase a failed listing must NOT paint the hard "Failed to
  // list" error: stat is still resolving and will drive the correct final view
  // (a file <Preview>) a beat later, so we show the neutral loading body and
  // let stat commit the real view. Absent/false (the committed post-stat
  // render), errors show normally.
  provisional?: boolean;
  // `embedded`: this Listing renders INSIDE another view (the preview pane's
  // `_listing` mode), not as the shell's main view. It must not touch the
  // address bar (no sort/q/sel URL reflection), never opens its own
  // preview pane (no nesting), and registers no document-level keyboard
  // handlers — those belong to the host's Listing. Mouse interaction stays:
  // clicks select/navigate, right-click menus and dialogs work as usual.
  embedded?: boolean;
  // Reports the path of this directory's lone top-level HTML file (an
  // "app"), or null when there isn't exactly one — the caller (Preview's
  // header) uses this to surface an "Open as app" button. Fires whenever the
  // plain (non-search) listing settles, so it tracks dir-watch refreshes too.
  onSingleApp?: (path: string | null) => void;
}) {
  const { state, refresh, refetch, loadMore, loadingMore, newNames } =
    useDirListing(fsPath);

  // Sort lives in the URL; mirror it in state so clicks re-render without a
  // navigation (vanilla re-ran renderListing after its replaceState).
  const [{ sort, order }, setSortState] = useState<{
    sort: SortKey;
    order: SortOrder;
  }>(() => resolveSort(fsPath, !embedded));
  // When the sort was restored from saved state (URL carried none), reflect it
  // in the URL so the address bar, bookmarks, and Back-button history match
  // what's shown — as if the column had been clicked. Only syncs a genuinely
  // saved order; an unsorted folder keeps its clean, param-free URL. replaceState
  // (not navigate) so the view doesn't remount.
  useEffect(() => {
    if (embedded) return; // the URL belongs to the host view
    if (new URLSearchParams(location.search).get("sort")) return; // URL is authoritative
    const s = new URLSearchParams(getViewState(fsPath));
    // No stored SORT → leave default sort + clean URL. (The stored string may
    // still carry pane keys — those never ride the URL.)
    if (!s.get("sort")) return;
    const params = new URLSearchParams(location.search);
    params.set("sort", s.get("sort") || "name");
    params.set("order", s.get("order") === "desc" ? "desc" : "asc");
    replaceSearch(location.pathname + "?" + params.toString());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fsPath]);
  const setSort = (key: SortKey) => {
    const next: { sort: SortKey; order: SortOrder } = {
      sort: key,
      order: key === sort && order === "asc" ? "desc" : "asc",
    };
    if (embedded) {
      // Pane-local: no URL write, no persisted per-folder choice — a glance
      // in the preview must not re-sort the folder's real listing later.
      setSortState(next);
      return;
    }
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

  const {
    query,
    setQuery,
    searching,
    isStale,
    validWalk,
    prefetchWalk,
    hits,
    displayHits,
    visibleHits,
    showingHeld,
    hasMore,
    sentinelRef,
    searchSort,
    setSearchSort,
    setSearchSortKey,
  } = useWalkSearch(fsPath, refresh, !embedded);

  // An embedded Listing never opens its own pane (no nesting): the feature is
  // disabled at the hook, however wide the embedded listing gets. Otherwise
  // `pane.on` is purely a measurement of the split container (see pane.ts).
  const { pane, splitRef, onDividerPointerDown } = usePreviewPane(
    fsPath,
    !embedded
  );

  const clipboard = useClipboard();

  // Search input, so a keystroke anywhere in the listing can focus it.
  const searchInputRef = useRef<HTMLInputElement>(null);
  // Path -> RowCtx for the rendered rows, read by the once-registered keydown
  // handler so Enter can pass the row's is_dir as a nav hint (assigned each
  // render from the rowCtxByPath memo below).
  const rowCtxByPathRef = useRef<Map<string, RowCtx>>(new Map());
  // True while a context menu or a modal dialog is open. The document-level nav
  // and shortcut handlers (registered once, reading refs) hard-guard on this so
  // an open overlay owns the keyboard — a stray Enter can't navigate a row and
  // Cmd+Backspace can't trash one behind the dialog, regardless of where focus
  // sits (the dialog's own containment covers focus; this covers the rest).
  const overlayOpenRef = useRef(false);

  // Same idea for the plain (non-search) listing as visibleHits' memo:
  // re-sorting on every render (e.g. a keystroke that flips `searching` before
  // this branch even displays) was pure waste when `state`/sort/order hadn't
  // changed.
  const sortedEntries = useMemo(
    () =>
      state.status === "ok" ? sortEntries(state.entries, sort, order) : [],
    [state, sort, order],
  );

  const base = fsPath.replace(/\/$/, "");

  // "Up" navigation for the button beside the search box: hop to the parent
  // folder. It used to also seed `?sel=<name>` there so the row you came from
  // was highlighted; that param is gone (useListingSelection documents why), so
  // the parent lands on its first entry like any other folder open. Disabled at
  // the filesystem / drive root, where dirname collapses to the folder itself.
  const here = normDir(base);
  const parentDir = dirname(here);
  const atRoot = parentDir === here;
  const goUp = () => {
    if (atRoot) return;
    navigate(parentDir, { isDir: true });
  };

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
    [searching, visibleHits, sortedEntries, base],
  );

  // Whether navRows reflects a LOADED listing (not a transient empty while the
  // fetch is in flight). Only the non-search listing can be mid-load with rows
  // still empty AND a selection already set — that's the folder-open case: the
  // resolved Listing mounts with a selection restored from the pre-stat
  // provisional one, but its own /api/fs/list is briefly loading. Search keeps
  // its prior behavior (results stream in). Used by the reconcile effect so a
  // real, still-valid selection is never cleared as "vanished" during a reload.
  // "Settled" = not mid-fetch: an ok listing OR a terminal error (rows are
  // then genuinely empty, so the reconcile should clear/reclamp a stale
  // selection). Only the transient `loading` status suppresses reconcile.
  const listingLoaded = searching ? true : state.status !== "loading";

  const {
    sel,
    selectedPath,
    selectedSet,
    selectOnly,
    toggleSelected,
    extendTo,
    pendingSelectRef,
  } = useListingSelection({
    fsPath,
    navRows,
    listingLoaded,
    searchInputRef,
    rowCtxByPathRef,
    overlayOpenRef,
    globalKeys: !embedded,
  });

  const {
    menu,
    setMenu,
    dialog,
    setDialog,
    doPaste,
    doDuplicate,
    doTrash,
    startRename,
    startNewFolder,
    rowMenu,
    backgroundMenu,
  } = useFileOps({ base, clipboard, refetch, pendingSelectRef });

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

  // FLIP the rows to their new slots whenever the rendered set changes: a column
  // sort, a dir-watch refresh of the plain listing, or a streaming search
  // re-rank (which B4 throttles, so the glide has time to read). navRows is the
  // rendered order itself, so one signal covers all three; growing it by a page
  // moves nothing already on screen, so paging animates nothing.
  const scrollRef = useRef<HTMLDivElement>(null);
  useFlip(scrollRef, navRows, navRows.length <= FLIP_MAX_ROWS);

  // Scroll anchoring (B5). A dir-watch refresh that inserts or removes rows
  // ABOVE the viewport shifts everything below it, so the rows the user was
  // reading slid out from under them. Re-apply the scroll offset the anchor row
  // had. The anchor is re-measured on EVERY commit (it has to be current), but
  // the correction is applied only when the refresh generation changed: a sort
  // or a page reveal is the user's own gesture and must not be undone.
  const anchorRef = useRef<{
    key: string;
    top: number;
    scrollTop: number;
  } | null>(null);
  const anchorGenRef = useRef(refresh);
  useLayoutEffect(() => {
    const scroller = scrollRef.current;
    if (!scroller) return;
    const prev = anchorRef.current;
    if (prev && refresh !== anchorGenRef.current) {
      const el = scroller.querySelector<HTMLElement>(
        `[${FLIP_KEY_ATTR}="${CSS.escape(prev.key)}"]`,
      );
      if (el) {
        const shift = el.offsetTop - prev.top;
        if (shift !== 0) scroller.scrollTop = prev.scrollTop + shift;
      }
    }
    anchorGenRef.current = refresh;
    anchorRef.current = measureScrollAnchor(scroller);
  }, [navRows, refresh]);

  // Which visible entries are cut sources — dimmed in the table. A cut can hold
  // several paths, so this is a set rather than one path.
  const cutSet = useMemo(
    () => new Set(clipboard?.op === "cut" ? clipboard.paths : []),
    [clipboard],
  );

  // The copy counterpart: marked with an accent edge + wash rather than dimmed
  // (a copy doesn't remove anything, so fading the source would lie). Exactly
  // one of cutSet/copiedSet is ever non-empty — the clipboard holds one op.
  const copiedSet = useMemo(
    () => new Set(clipboard?.op === "copy" ? clipboard.paths : []),
    [clipboard],
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

  // Opening a folder lands on its FIRST ENTRY — file or directory (rendered
  // order — see autoSelectPath / firstEntryPath), so the pane shows something
  // instead of the folder's own "Select a file to preview." hint. A pane that opens empty
  // asks the user to do the obvious thing before it will do anything at all; a
  // folder is overwhelmingly opened to look at what is in it.
  //
  // ONE SHOT, and this effect owns only the TIMING of it — autoSelectPath owns
  // the decision. The shot is taken at the first settled non-search listing WITH THE
  // PANE ON. Listing remounts per fsPath, so that is once per folder
  // navigation: a dir-watch refresh never re-fires it, and a selection the user
  // cleared (Escape) stays cleared.
  //
  // Three conditions hold the shot rather than spending it, because each can
  // still turn into a folder the user is looking at:
  //   • the pane is OFF — the container is too narrow to split, so there is
  //     nothing to preview into yet; widening the window later should still
  //     land on the first entry (`pane.on` is a dependency for exactly that);
  //   • search mode — the rendered rows are a query's answer, not the folder's,
  //     so clearing the query still lands on the folder's first entry;
  //   • the listing is not OK — `status !== "ok"` and not merely "still
  //     loading". A failed first fetch settles with zero rows, and the
  //     dir-watch refetch that succeeds afterwards does NOT pass back through
  //     "loading" — so spending the shot on the error state meant a folder
  //     whose first request blipped never auto-selected at all, for the whole
  //     mount. `listingLoaded` (which is true for a terminal error, by design —
  //     the selection reconcile WANTS to clear a stale selection there) is the
  //     wrong question for this effect.
  // TWO Listings never fire at all:
  //   • an EMBEDDED one (the pane's own `_listing` mode) — it has no pane of
  //     its own to fill;
  //   • a PROVISIONAL one (App's pre-stat loading scaffold, mounted off a nav
  //     hint). Auto-selecting there mounts a real /render iframe that the swap
  //     to the resolved Listing tears down and re-issues a beat later — a
  //     doubled stat and a doubled frame load on every folder navigation, for a
  //     preview nobody saw. The scaffold's job is to hold the SHAPE (the split,
  //     the divider, the pane's chrome) so nothing jumps when the real listing
  //     lands; it is not the place to start work that is about to be thrown
  //     away. The pane itself stays — only the automatic selection waits. A
  //     user's own click in the scaffold still previews, and still carries
  //     across the swap (recallSelection), because that one was asked for.
  //
  // And it FILLS AN EMPTY SELECTION, never replaces one: the shot is spent
  // silently when something already holds the selection at the moment the
  // guards are first met (`selectionClaimed`). That is exactly the scaffold
  // click above — the user clicked row five during a slow mount, the resolved
  // listing settled, and row one used to land on top of it. The decision half
  // (autoSelectPath) stays blind to the selection (D240); this is a condition
  // on WHEN to ask, which is this effect's half.
  const autoSelectedRef = useRef(false);
  useEffect(() => {
    if (embedded || provisional || autoSelectedRef.current) return;
    if (searching || state.status !== "ok" || !pane.on) return;
    autoSelectedRef.current = true;
    if (selectionClaimed(sel)) return;
    const first = autoSelectPath(navRows, rowCtxByPath);
    if (first) selectOnly(first);
    // Fires on the commit that first satisfies the guards above; the rows it
    // reads are current as of that commit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [embedded, provisional, searching, state.status, pane.on]);

  // The selection as full rows, in rendered order (so a batch op processes rows
  // top-to-bottom regardless of the order they were clicked). Paths without a
  // rendered row — a search page not yet revealed, a row removed by a refetch
  // before the reconcile effect ran — are dropped: an op can only act on what
  // the user can actually see selected.
  const selectedRows = useMemo(() => {
    const chosen = new Set(sel.paths);
    return navRows
      .filter((p) => chosen.has(p))
      .map((p) => rowCtxByPath.get(p)!)
      .filter(Boolean);
  }, [sel.paths, navRows, rowCtxByPath]);
  // The lead row, for the single-entry operations (Rename, paste target).
  const leadRow = sel.lead ? rowCtxByPath.get(sel.lead) : undefined;

  useListingShortcuts({
    base,
    clipboard,
    selectedRows,
    leadRow,
    searchInputRef,
    overlayOpenRef,
    doPaste,
    doDuplicate,
    doTrash,
    startRename,
    startNewFolder,
    globalKeys: !embedded,
  });

  // Mouse selection on a row:
  //   • Shift+click  — select the contiguous range anchor..row (rendered order);
  //   • Mod+click    — toggle this row in/out and re-anchor on it;
  //   • plain click  — depends on the preview pane. Pane OFF (the default):
  //     select AND open, what a single click has always done in this explorer.
  //     Pane ON: select only — the click's job is to drive the pane preview
  //     (files and folders both), and double-click is what opens. Enter still
  //     opens either way (the keyboard model doesn't change with the pane).
  // No single/double-click delay timer: with the pane on, the first click of a
  // double-click selects (harmless — the pane fetch is superseded/unmounted by
  // the navigation the second click triggers).
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
    if (!pane.on) navigate(row.path, { isDir: row.isDir });
  };

  // Double-click opens when the pane owns the single click. Pane off: the
  // single click already navigated, so this is a no-op (navigation unmounts
  // the listing before a second click can land anyway).
  const onRowDoubleClick = (row: RowCtx) => {
    if (pane.on) navigate(row.path, { isDir: row.isDir });
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
    const winSel = window.getSelection();
    if (winSel && !winSel.isCollapsed) winSel.removeAllRanges();
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

  // --- table body -----------------------------------------------------------

  let body: React.ReactNode;
  // Column headers describe columns of data; over an empty folder they label
  // nothing and just push the "Empty directory" message down (most visible in
  // the preview pane, where NAME/SIZE/MODIFIED sat above one line of text).
  // Set by the empty branch below, read by the <thead> render.
  let emptyDir = false;
  if (searching) {
    if (validWalk.status === "error") {
      body = (
        <tr>
          <td colSpan={3} className="status-message error">
            Search failed: {validWalk.message}
          </td>
        </tr>
      );
    } else if (displayHits.length) {
      // Rows exist: either the fresh ranking, or the held one from the same
      // query while a refresh-invalidated walk re-runs (showingHeld).
      body = (
        <>
          {visibleHits.map(({ entry, positions }) => {
            const childPath = base + "/" + entry.rel;
            return (
              <tr
                key={entry.rel}
                data-flip-key={childPath}
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
                onDoubleClick={() =>
                  onRowDoubleClick({
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
                  <span className="icon">
                    {iconForEntry(
                      entry.rel.split("/").pop() ?? entry.rel,
                      entry.is_dir,
                    )}
                  </span>
                  <span className="search-path">
                    {renderHighlight(entry.rel, positions)}
                  </span>
                  <ClipMark
                    cut={cutSet.has(childPath)}
                    copied={copiedSet.has(childPath)}
                  />
                </td>
                <td className="size">
                  {entry.is_dir ? "" : formatSize(entry.size)}
                </td>
                <td className="mtime" title={formatMtimeFull(entry.mtime)}>
                  {formatMtime(entry.mtime)}
                </td>
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
    } else if (validWalk.status === "ok" || validWalk.status === "streaming") {
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
          data-flip-key={childPath}
          className={
            (entry.ignored ? "row ignored" : "row") +
            (newNames.has(entry.name) ? " row-new" : "") + // brief dir-watch tint
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
          onDoubleClick={() =>
            onRowDoubleClick({
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
            <span className="icon">
              {iconForEntry(entry.name, entry.is_dir)}
            </span>
            {entry.name}
            <ClipMark
              cut={cutSet.has(childPath)}
              copied={copiedSet.has(childPath)}
            />
          </td>
          <td className="size">{entry.is_dir ? "" : formatSize(entry.size)}</td>
          <td className="mtime" title={formatMtimeFull(entry.mtime)}>
            {formatMtime(entry.mtime)}
          </td>
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
          Showing first {sortedEntries.length} entries — directory listing is
          partial.
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
    emptyDir = !rows.length && !banner;
    body = emptyDir ? (
      <tr>
        <td colSpan={3} className="status-message">
          Empty directory
        </td>
      </tr>
    ) : (
      <>
        {rows}
        {banner}
      </>
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

  // Is anything pinned inside the search input right now? Mirrors the three
  // chip conditions in the render below; drives the input's right padding, so
  // an idle box gives its whole width to the placeholder.
  const hasPin =
    (searching && (validWalk.status === "idle" || validWalk.status === "streaming")) ||
    searchCount !== null ||
    sel.paths.length > 1;

  return (
    <div className="listing">
      <div className="listing-split" ref={splitRef}>
        <div className="listing-main">
          {/* Embedded (preview pane): no search row — the pane is a glance,
              and the host listing's search/toggle already own that chrome. */}
          {!embedded && (
            <div className="listing-search">
              <button
                type="button"
                className="bar-ctl bar-ctl-icon"
                title={atRoot ? "Already at the root" : "Up to parent folder"}
                aria-label="Up to parent folder"
                disabled={atRoot}
                onClick={goUp}
              >
                <svg
                  viewBox="0 0 24 24"
                  width="16"
                  height="16"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <line x1="12" y1="19" x2="12" y2="5" />
                  <polyline points="5 12 12 5 19 12" />
                </svg>
              </button>
              {/* The box wraps input + pinned chips so the pane toggle can sit to
            their right without disturbing the chips' inside-the-input pin.
            `has-pin` says a chip is actually pinned right now, so the input
            reserves room for one only then — the reservation is wide, and
            idle it was dead space that clipped the placeholder in a narrow
            window. */}
              <div className={"listing-search-box" + (hasPin ? " has-pin" : "")}>
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
                {searching &&
                  (validWalk.status === "idle" ||
                    validWalk.status === "streaming") && (
                    <span
                      className="listing-search-spinner"
                      aria-hidden="true"
                    />
                  )}
                {searchCount !== null && (
                  <span
                    className="listing-search-count"
                    title={searchCountTitle}
                  >
                    {searchCount}
                  </span>
                )}
                {/* Multi-selection readout — a single selected row needs no count. */}
                {sel.paths.length > 1 && (
                  <span className="listing-search-count">
                    {sel.paths.length} selected
                  </span>
                )}
              </div>
              {/* Current result ordering, and the way back to relevance. Without it
            "no arrow anywhere" was the only signal that results were in fuzzy
            rank order, and a column sort had no explicit escape. */}
              {searching && (
                <button
                  type="button"
                  className={
                    "listing-sort-chip" + (searchSort ? " sorted" : "")
                  }
                  disabled={!searchSort}
                  title={
                    searchSort
                      ? "Results are column-sorted — click for relevance order"
                      : "Results are in relevance order (best match first)"
                  }
                  onClick={() => setSearchSort(null)}
                >
                  {searchSort
                    ? `${SORT_KEYS[searchSort.sort].toLowerCase()} ${searchSort.order}`
                    : "relevance"}
                </button>
              )}
              {/* No pane toggle here any more. The split is decided by the
                  container's width (listing/pane.ts), so there is no state for
                  a button to flip — and a control that only ever restated what
                  the layout already showed was one more thing in a row that is
                  meant to be the search box. */}
            </div>
          )}
          <div
            ref={scrollRef}
            /* Dimmed both when the deferred render lags a keystroke and while
             held (pre-refresh) results stand in for a re-running walk. */
            className={
              "listing-scroll" +
              (isStale || showingHeld ? " listing-stale" : "")
            }
            /* No onClick here: clicking the empty area below the rows does
               NOT deselect. Finder's rule, and it cost more than it bought
               once the preview pane arrived — a stray click anywhere in the
               whitespace of a short listing blanked the pane and threw away
               the row the user was reading. Escape still clears (the
               deliberate gesture); the background is just background. */
            onContextMenu={openBackgroundMenu}
          >
            <table className="listing-table">
              {/* Header row hidden (not unmounted — the sticky header's box is
                  part of the table's own layout) over an empty folder: see
                  `emptyDir`. */}
              <thead className={emptyDir ? "listing-head-empty" : undefined}>
                <tr>
                  {(Object.entries(SORT_KEYS) as [SortKey, string][]).map(
                    ([key, label]) =>
                      searching ? (
                        // While searching, headers sort the results; no active arrow
                        // means relevance (fuzzy-rank) order.
                        <th
                          key={key}
                          className={
                            `sortable col-${key}` +
                            (searchSort?.sort === key ? " sorted" : "")
                          }
                          title={
                            searchSort?.sort === key &&
                            searchSort.order === "desc"
                              ? `Back to relevance order`
                              : `Sort results by ${label.toLowerCase()}`
                          }
                          onClick={() => setSearchSortKey(key)}
                        >
                          {label}
                          {/* One glyph that ROTATES for desc (see .sort-arrow):
                          swapping ▲ for ▼ replaced the element, so the change
                          could only ever pop. */}
                          {searchSort?.sort === key && (
                            <span
                              className={
                                "sort-arrow" +
                                (searchSort.order === "desc" ? " desc" : "")
                              }
                            >
                              ▲
                            </span>
                          )}
                        </th>
                      ) : (
                        <th
                          key={key}
                          className={
                            `sortable col-${key}` +
                            (key === sort ? " sorted" : "")
                          }
                          onClick={() => setSort(key)}
                        >
                          {label}
                          {key === sort && (
                            <span
                              className={
                                "sort-arrow" + (order === "desc" ? " desc" : "")
                              }
                            >
                              ▲
                            </span>
                          )}
                        </th>
                      ),
                  )}
                </tr>
              </thead>
              <tbody>{body}</tbody>
            </table>
          </div>
        </div>
        {pane.on && (
          <>
            <div
              className="listing-divider"
              onPointerDown={onDividerPointerDown}
              role="separator"
              aria-orientation="vertical"
            />
            <div
              className="listing-pane-slot"
              // A PERCENTAGE, not a pixel width: the split is stored as a
              // fraction of this container (listing/pane.ts), so a window
              // resize keeps the proportion the user dragged instead of
              // leaving the pane at one window's arithmetic. The pixel floors
              // are the slot's / the list's CSS min-widths.
              style={{ flexBasis: `${pane.frac * 100}%` }}
            >
              {/* Keyed on the previewed path: switching rows remounts the pane,
                  so a stale iframe never lingers a frame while the new row's
                  stat/list resolves. Nothing selected → the pane previews THIS
                  folder itself (self: its template or lone app — never its
                  listing, which is already on the left). */}
              <ListingPreviewPane
                key={
                  sel.paths.length === 1 && leadRow
                    ? leadRow.path
                    : sel.paths.length === 0
                      ? "self:" + fsPath
                      : "none"
                }
                row={
                  sel.paths.length === 1 && leadRow
                    ? leadRow
                    : sel.paths.length === 0
                      ? {
                          path: fsPath,
                          name:
                            fsPath.replace(/\/+$/, "").split("/").pop() ||
                            fsPath,
                          isDir: true,
                          self: true,
                        }
                      : null
                }
                selCount={sel.paths.length}
              />
            </div>
          </>
        )}
      </div>

      {menu && (
        <ContextMenu
          x={menu.x}
          y={menu.y}
          items={menu.items}
          onClose={() => setMenu(null)}
        />
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
