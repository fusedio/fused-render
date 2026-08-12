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
//   pane-side.ts           the pane's three modes + the `_side` param (pure)
//   row-utils.ts           RowCtx batch helpers
//   bits.tsx               skeleton rows, ClipMark, highlight, scroll anchor
//   useDirListing.ts       /api/fs/list fetch, Load more, dir watch, new-row cue
//   useWalkSearch.ts       streamed walk + scoring + throttles + result paging
//   useListingSelection.ts selection state + keyboard nav + reconcile
//   useFileOps.ts          file operations + context menus + dialogs
//   drag-drop.ts           what a drag carries + which drops are legal (pure)
//   marquee.ts             sweep-to-select geometry: region, hits, auto-scroll (pure)
//   useMarquee.ts          the press ARBITER (sweep vs move) + the sweep itself
//   row-drag.ts            the move-drag: pointer tracking, targets, the ghost
//   useRowDrag.ts          what a press picks up + who performs the drop
//   shortcut-chord.ts       which chord means which action (pure)
//   useListingShortcuts.ts file-op keyboard chords
import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { IS_SNAPSHOT, navigate, replaceSearch } from "@platform/lib/router";
import { dirname, normDir } from "@apps/explorer/lib/fs-actions";
import { acquireOverlay, releaseOverlay } from "@platform/lib/ui-overlay";
import { isMod } from "@platform/lib/platform";
import { formatSize, formatMtime, formatMtimeFull } from "@platform/lib/format";
import { iconForEntry } from "@platform/ui/FileIcons";
import { getViewState, setViewState } from "@platform/lib/viewstate";
import { useFlip, FLIP_KEY_ATTR } from "@platform/lib/flip";
import { useClipboard } from "@apps/explorer/lib/fs-clipboard";
import ContextMenu from "@platform/ui/ContextMenu";
import { SplitDownIcon, SplitRightIcon } from "@platform/ui/SplitIcons";
import { EllipsisIcon } from "@apps/explorer/BarMenu";
import { enterPanel } from "@apps/explorer/lib/split-actions";
import { PromptDialog, ConfirmDialog } from "@apps/explorer/FsDialogs";
import ListingPreviewPane from "@apps/explorer/ListingPreviewPane";
import { resultCountLabel } from "@apps/explorer/listing/result-cap";
import { claimFolderChrome } from "@apps/explorer/listing/folder-chrome";
import { searchSlot, subscribeSearchSlot } from "@apps/explorer/search-slot";
import {
  FLIP_MAX_ROWS,
  SORT_KEYS,
  columnCount,
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
import {
  activePaneSide,
  paneKey,
  paneSideList,
  paneSideParam,
  parsePaneSide,
  type PaneSide,
  type PaneSideState,
} from "@apps/explorer/listing/pane-side";
import { useDirMode } from "@apps/explorer/lib/dir-mode";
import { SideToggleButton, paneSideIcon } from "@apps/explorer/SideChrome";
import { modeTitle } from "@platform/lib/mode-name";
import { passedDragSlop } from "@apps/explorer/listing/marquee";
import {
  INITIAL_SEARCH_SELECT,
  autoSelectPath,
  nextSearchSelection,
  rowPressAction,
  selectionClaimed,
} from "@apps/explorer/listing/selection";
import { useRowDrag } from "@apps/explorer/listing/useRowDrag";
import { useMarquee } from "@apps/explorer/listing/useMarquee";
import { useDirListing } from "@apps/explorer/listing/useDirListing";
import { useWalkSearch } from "@apps/explorer/listing/useWalkSearch";
import { useIndexStatus } from "@platform/lib/index-status";
import { indexCaveat, withCaveat } from "@apps/explorer/listing/index-caveat";
import { useListingSelection } from "@apps/explorer/listing/useListingSelection";
import { useFileOps } from "@apps/explorer/listing/useFileOps";
import { useListingShortcuts } from "@apps/explorer/listing/useListingShortcuts";

// The search row hangs in the crumb bar when there is one to hang in, and
// stays put otherwise. Either way it is the SAME React element — the query,
// the walk's live counts and `searchInputRef` are Listing's state, and a
// portal moves the DOM without touching any of that (a keystroke that focuses
// the box from the listing below still reaches it).
function inSearchSlot(slot: HTMLElement | null, row: ReactNode): ReactNode {
  return slot ? createPortal(row, slot) : row;
}

export default function Listing({
  fsPath,
  provisional = false,
  embedded = false,
  barChrome = false,
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
  // `barChrome`: this Listing IS the explorer's folder view — the one under
  // the crumb bar, whose layout zone it therefore claims (see
  // listing/folder-chrome.ts). The splits go away and the path `···` renders
  // in this listing's search row instead of at the far end of the bar. False
  // for every other host: the learn variant has no crumb bar to claim, and a
  // panel pane's Listing sits under a pane bar that carries its own splits and
  // its own `···`.
  barChrome?: boolean;
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
    scanPending,
    validWalk,
    prefetchWalk,
    hits,
    displayHits,
    visibleHits,
    showingHeld,
    cappedAway,
  } = useWalkSearch(fsPath, refresh, !embedded);

  // Scan state for the search box's "indexing…" caveat. Gated on `searching`
  // so an idle listing never polls.
  const indexScan = useIndexStatus(searching);

  // An embedded Listing never opens its own pane (no nesting): the feature is
  // disabled at the hook, however wide the embedded listing gets. Otherwise
  // `pane.on` is purely a measurement of the split container (see pane.ts).
  //
  // A FROZEN-TREE listing is the second no-nesting case, and `embedded` cannot
  // see it: the browsable snapshot (`history`'s `browse` framing, PT-14) is a
  // whole shell loaded at `/explorer/embed/<tree>?snapshot=1`, so its Listing
  // is the page's OWN top-level one — `embedded=false` — inside the history
  // view's preview column. That column is 70% of the window, which on any
  // ordinary screen is comfortably past PANE_SPLIT_MIN_W (measured: 954px in a
  // 1600px window), so the frozen listing grew a preview pane INSIDE a preview
  // pane. `?preview=false` used to stop it and was dropped with the toggle it
  // belonged to, on the reasoning that the width decides — true for a listing
  // that owns its window, false for one handed a column by a framer.
  //
  // `snapshot=1` and not a second param of its own: the framing flag has
  // exactly one producer, and that producer is a template framing this listing
  // in its own column. A flag that could only ever be written beside another
  // one is the "three places to agree about one bit" the pane's own history
  // (pane.ts) is a warning about.
  const paneEnabled = !embedded && !IS_SNAPSHOT;
  const { pane, splitRef, onDividerPointerDown } = usePreviewPane(paneEnabled);

  // --- the pane's THREE modes, and whether it is open at all ------------------
  // `pane.on` above is the LAYOUT's answer ("is there room for two columns?",
  // pane.ts) and is not a choice. This is the user's, on top of it: which of the
  // pane's three modes it is showing, or that they have shut it — recorded as
  // `_side` on the folder URL, whose semantics (and why an ABSENT one means OPEN
  // here while it means CLOSED on a file view) are written down in
  // listing/pane-side.ts.
  //
  // The mode is kept here and not in the pane because the pane is keyed on the
  // previewed row and remounts as the selection moves, while a chosen mode must
  // not; and because the reopening half of the affordance has to render while
  // the pane does not exist at all (see the search row below).
  const [sideState, setSideState] = useState<PaneSideState>(() =>
    parsePaneSide(paneEnabled ? new URLSearchParams(location.search).get("_side") : null)
  );
  // Both companions' entries come from the OPEN FOLDER, resolved through the
  // ordinary stat + condition machinery (lib/dir-mode — which caches per
  // directory, so this is one probe for the folder rather than one per selection).
  // `git` because a working tree belongs to the folder; `claude` because the
  // pane's chat is the FOLDER VIEW's companion, aimed at whichever row is
  // selected, so which chat template to use is a question about the folder too.
  //
  // A folder outside a repository loses the Git pill, and one on a mount loses
  // both (each gate refuses a mount-backed path) — at which point the pill hides
  // itself, "one mode is not a choice", and the pane is what it always was.
  const folderClaude = useDirMode(paneEnabled ? fsPath : null, "claude");
  const folderGit = useDirMode(paneEnabled ? fsPath : null, "git");
  // While the probe is in flight the entries are PLACEHOLDERS with no template
  // path (lib/dir-mode), which would build a `path=null` iframe URL — so a
  // pending companion is simply not offered yet. Unlike the file sidebar there is
  // nothing to protect by listing it early: the folder's `_side` is never
  // reconciled away (pane-side's activePaneSide leaves an unavailable request in
  // the URL on purpose), so a `?_side=git` deep link survives the wait and lands
  // the moment the verdict does.
  const sideEntries = {
    claude: folderClaude.pending ? null : folderClaude.entry,
    git: folderGit.pending ? null : folderGit.entry,
  };
  const paneSide = activePaneSide(paneSideList(sideEntries), sideState.mode);
  const paneOpen = pane.on && sideState.open;
  // One writer for both halves of the state, and it writes the URL only where the
  // listing owns one: an embedded pane (the preview pane's own `_listing` mode) is
  // URL-silent by contract, and it never has a pane of its own anyway.
  const setSide = (next: PaneSideState) => {
    setSideState(next);
    if (!paneEnabled) return;
    const params = new URLSearchParams(location.search);
    const v = paneSideParam(next);
    if (v === null) params.delete("_side");
    else params.set("_side", v);
    const qs = params.toString();
    replaceSearch(location.pathname + (qs ? "?" + qs : ""));
  };
  // Reopening keeps the mode the pane was shut on, so closing and reopening is
  // not a reset. Session-only — see paneSideParam on why the URL records only
  // "shut".
  const openSide = () => setSide({ open: true, mode: sideState.mode });
  const closeSide = () => setSide({ open: false, mode: sideState.mode });
  const selectSide = (mode: PaneSide) => setSide({ open: true, mode });

  const clipboard = useClipboard();

  // Search input, so a keystroke anywhere in the listing can focus it.
  const searchInputRef = useRef<HTMLInputElement>(null);
  // --- resting search box folds to a magnifier on a tight bar ---------------
  // The bar's yield order (crumbs shrink → box shrinks, explorer.css) bottoms
  // out with the PATH still ellipsized on a narrow middle column, while the
  // idle box holds ~100px of placeholder. Past that point the box becomes a
  // 28px icon and the path gets the strip back. `tightBar` is the measured
  // fact; `pinnedOpen` is the user overriding it (clicked the icon — the box
  // stays until it blurs empty). A non-empty query outranks both: `.searching`
  // already stands the crumbs down and takes the whole strip.
  const [tightBar, setTightBar] = useState(false);
  const [pinnedOpen, setPinnedOpen] = useState(false);
  const searchRowRef = useRef<HTMLDivElement>(null);
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

  // Claim the crumb bar for as long as this folder view is mounted: the splits
  // come off it, the path `···` renders in the search row below, and the bar
  // itself portals into `crumbSlotRef` — the top of THIS column — so the
  // preview pane beside it runs the full height of the window (see
  // listing/folder-chrome.ts).
  //
  // A layout effect: the claim moves the bar, and a passive effect would paint
  // one frame with it still spanning the window before it dropped into place.
  // Refs are attached before layout effects run, so the slot is there.
  const ownsBarChrome = barChrome && !embedded;
  const crumbSlotRef = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    if (!ownsBarChrome) return;
    return claimFolderChrome(crumbSlotRef.current);
  }, [ownsBarChrome]);

  // …and the search row goes UP into that same bar, at its right end — one
  // header strip in this column, matching the pane's one across the divider
  // (search-slot.ts). Non-null only once the bar has rendered its target,
  // which is only ever over a folder that claimed the chrome; a host with no
  // crumb bar (the app builder) keeps the row in place as its own first strip.
  const barSearchSlot = useSyncExternalStore(subscribeSearchSlot, searchSlot, () => null);

  // The tight-bar measurement. DOM-side on purpose: this row PORTALS into
  // #breadcrumb (the slot above), so the crumbs it shares the strip with are
  // reachable — and already coupled to this row by the bar's :has() rules.
  // Two thresholds, deliberately apart, so the flip cannot oscillate:
  //   • fold: the crumbs are ellipsized (scrollWidth past clientWidth) even
  //     after the CSS yield order has bottomed out — the box is the only
  //     slack left to give.
  //   • unfold: the whole path is showing AND the bar has ≥150px genuinely
  //     free — room the full resting box (needing a net ~120px over the
  //     magnifier) can take without re-truncating anything. Free space is
  //     summed from the bar's visible children, not read off the crumbs:
  //     they are flex-grow 0 in slot mode, so their clientWidth hugs their
  //     content and never reports the strip's slack.
  // No dependency array: crumbs content changes with navigation but their
  // clientWidth may not, so a ResizeObserver alone misses scrollWidth-only
  // changes; re-measuring on every render is cheap and the guarded setState
  // converges. Skipped while the user is in the box — measuring a strip the
  // crumbs have stood down from (.searching hides them) reads zeros.
  useLayoutEffect(() => {
    if (embedded || searching || pinnedOpen) return;
    const row = searchRowRef.current;
    const bar = row?.closest("#breadcrumb");
    const crumbs = bar?.querySelector(".crumbs");
    if (!(bar instanceof HTMLElement) || !crumbs) return;
    const freeInBar = () => {
      // The bar's actual flex ITEMS, not bar.children: the search slot is
      // `display: contents` (its rect reads 0), so its children participate
      // in the bar's layout directly and must be counted in its place —
      // skipping them overstates the free space by the whole search row.
      const kids: Element[] = [];
      const collect = (el: Element) => {
        for (const child of Array.from(el.children)) {
          const d = getComputedStyle(child).display;
          if (d === "none") continue;
          if (d === "contents") collect(child);
          else kids.push(child);
        }
      };
      collect(bar);
      const cs = getComputedStyle(bar);
      const gap = parseFloat(cs.columnGap) || 0;
      const used =
        kids.reduce((w, el) => w + el.getBoundingClientRect().width, 0) +
        gap * Math.max(0, kids.length - 1) +
        (parseFloat(cs.paddingLeft) || 0) +
        (parseFloat(cs.paddingRight) || 0);
      return bar.clientWidth - used;
    };
    const measure = () => {
      setTightBar((folded) =>
        folded
          ? crumbs.scrollWidth > crumbs.clientWidth + 1 || freeInBar() < 150
          : crumbs.scrollWidth > crumbs.clientWidth + 1
      );
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(bar);
    ro.observe(crumbs);
    return () => ro.disconnect();
  });

  // The pin is a request to type: focus follows it in the same interaction.
  useEffect(() => {
    if (pinnedOpen) searchInputRef.current?.focus();
  }, [pinnedOpen]);

  // No "Up" BUTTON beside the search box any more: the crumb strip above is
  // the same hop with a target the user can name, and the keyboard keeps its
  // own (Mod+Up / bare Backspace — see listing/useListingShortcuts).

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
    selectPaths,
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
    doMove,
    doUndo,
    doRedo,
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

  // `selectstart`, cancelled for the whole scroller. This is the half of the
  // text-selection suppression that used to be preventDefault-on-mousedown (see
  // onRowPointerDown): it says "no selection begins or extends in here" without
  // cancelling a mousedown default that a draggable row needs. Registered
  // natively because React has no synthetic onSelectStart. Nothing inside the
  // scroller is meant to be selectable — the rows already carry
  // `user-select: none` — so there is nothing to lose by refusing all of them.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const onSelectStart = (e: Event) => e.preventDefault();
    el.addEventListener("selectstart", onSelectStart);
    return () => el.removeEventListener("selectstart", onSelectStart);
  }, []);

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

  // Opening a folder lands on its first PAGE, or on its first entry — file or
  // directory — when it has none (rendered order both ways; the rule and its
  // reasons are on autoSelectPath). Either way the pane shows something instead
  // of the folder's own "Select a file to preview." hint. A pane that opens
  // empty asks the user to do the obvious thing before it will do anything at
  // all; a folder is overwhelmingly opened to look at what is in it.
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
  // listing settled, and the auto-selection used to land on top of it. The
  // decision half (autoSelectPath) stays blind to the selection (D240); this is
  // a condition on WHEN to ask, which is this effect's half.
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

  // Search results land on their TOP HIT, so Enter and the pane act on the
  // best match without the user having to reach for it first.
  //
  // Unlike the folder shot above this is NOT one-shot: a folder's rows settle
  // once per navigation, results re-rank on every keystroke. The decision
  // (searchAutoSelectPath) owns what to select; this owns only two things.
  //
  // WHEN to ask. Not while embedded (the pane's own `_listing` has no pane to
  // fill) and not provisional, matching the folder shot. It does NOT wait for
  // `pane.on`, which that one does: the folder case exists to fill the pane,
  // whereas a selected top hit is worth having for Enter and the arrow keys
  // whether or not the window is wide enough to preview it.
  //
  // Whose selection it is — and in particular that a user's choice OUTLIVES a
  // query change — is `nextSearchSelection`'s to track, not this effect's. It
  // lived here as a ref that got cleared per query, which quietly threw the
  // user's selection away on the next keystroke; it is state with rules, so it
  // belongs somewhere it can be tested.
  const searchSelectRef = useRef(INITIAL_SEARCH_SELECT);
  useEffect(() => {
    if (embedded || provisional || !searching) return;
    const { state, select } = nextSearchSelection(
      searchSelectRef.current,
      navRows,
      rowCtxByPath,
      sel,
    );
    searchSelectRef.current = state;
    if (select !== null) selectOnly(select);
  }, [embedded, provisional, searching, navRows, rowCtxByPath, sel, selectOnly]);

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

  // Drag-to-move. The selection is passed in RENDERED order (selectedRows), so
  // dragging a row that is part of it carries the whole thing top-to-bottom.
  // Rows carry no drag handlers: they declare what they ACCEPT with the
  // data-fs-drop-* attributes below, and the gesture itself is pointer-driven
  // (listing/row-drag.ts).
  const { startMoveDrag } = useRowDrag({
    selectedPaths: useMemo(() => selectedRows.map((r) => r.path), [selectedRows]),
    rowCtxByPath,
    scrollRef,
    onMove: doMove,
  });

  // The listing's ONE press arbiter, in the capture phase (see the wiring on the
  // scroller below). It decides sweep-versus-move from a snapshot of the
  // selection taken before the press can change it, then either sweeps here or
  // hands the move-drag over.
  //
  // The sweep writes through the ONE selection model that clicks and the
  // keyboard use (selectPaths above) — no parallel store, no second `?sel=`
  // writer — and it draws nothing: the rows lighting up as the pointer crosses
  // them is the feedback, which is precisely what made the old rubber band
  // redundant.
  const { onPointerDownCapture: onListingPointerDownCapture } = useMarquee({
    scrollRef,
    navRows,
    selectedPaths: sel.paths,
    selectPaths,
    startMoveDrag,
  });

  useListingShortcuts({
    base,
    clipboard,
    selectedRows,
    leadRow,
    searchInputRef,
    overlayOpenRef,
    doPaste,
    doUndo,
    doRedo,
    doDuplicate,
    doTrash,
    startRename,
    startNewFolder,
    globalKeys: !embedded,
  });

  // Mouse selection on a row — SELECTION ONLY, never navigation, and decided
  // on the PRESS:
  //   • Shift+press  — select the contiguous range anchor..row (rendered order);
  //   • Mod+press    — toggle this row in/out and re-anchor on it;
  //   • plain press  — select this row alone;
  //   • plain press already inside a MULTI-selection — nothing yet; see
  //     onRowPointerUp.
  // Which of the four a gesture means is listing/selection's rowPressAction,
  // where the model and the reason it hangs off pointerdown are written down
  // and tested. In short: rows are drag sources, a draggable element does not
  // reliably deliver the `click` after the press, and every selection path in
  // this listing used to hang off exactly that click.
  //
  // Left button only. The right button belongs to the context menu, which does
  // its own selection handling (openRowMenu below), and the middle button is
  // the browser's.
  const pressRef = useRef<{ path: string; x: number; y: number } | null>(null);

  const onRowPointerDown = (e: React.PointerEvent, path: string) => {
    if (e.button !== 0) return;
    const action = rowPressAction({
      mod: isMod(e),
      shift: e.shiftKey,
      inMultiSelection: selectedSet.has(path) && sel.paths.length > 1,
    });
    // Remembered for the deferred case only, but recorded for every press so
    // the release can measure how far the pointer travelled.
    pressRef.current = action === "defer" ? { path, x: e.clientX, y: e.clientY } : null;
    if (action === "defer") return;
    if (action === "select") {
      selectOnly(path);
      return;
    }
    collapseNativeSelection();
    if (action === "extend") extendTo(path);
    else toggleSelected(path);
  };

  // The deferred half: a plain press inside a multi-selection collapses onto
  // the pressed row when the button comes up, and ONLY if the press stayed
  // still. If it travelled, it was a drag of the whole selection (or a sweep)
  // and the selection is not ours to change.
  //
  // The distance test reuses the sweep's own slop rather than introducing a
  // second threshold — one number decides press-versus-gesture everywhere. A
  // press that became a native drag usually never delivers a pointerup at all,
  // so this mostly does not run in that case; the slop covers the rest,
  // including a drag the user cancelled.
  const onRowPointerUp = (e: React.PointerEvent, path: string) => {
    const press = pressRef.current;
    pressRef.current = null;
    if (!press || press.path !== path) return;
    if (passedDragSlop({ x: press.x, y: press.y }, { x: e.clientX, y: e.clientY })) return;
    selectOnly(path);
  };

  // Double-click OPENS. Unconditionally: the same gesture in the same folder
  // has to mean the same thing whether or not the window happens to be wide
  // enough for the preview pane (listing/selection documents the model). Enter
  // opens the same target from the keyboard.
  // No single/double-click delay timer: the first click of a double-click
  // selects, which is harmless — any pane fetch it starts is superseded or
  // unmounted by the navigation the second click triggers.
  const onRowDoubleClick = (row: RowCtx) => {
    navigate(row.path, { isDir: row.isDir });
  };

  // Kill the browser's own text selection for a Shift/Mod press.
  //
  // `user-select: none` on tr.row (shell.css) is necessary but NOT sufficient:
  // it makes the row's own text unselectable, yet a Shift+click still sets a
  // selection ENDPOINT, so the browser happily paints a range anchored at
  // whatever selectable text was last clicked (a crumb, the search box,
  // anything outside the table) straight across the listing.
  //
  // This used to be `preventDefault()` on the MOUSEDOWN, which is the earliest
  // moment and stops a range being started or extended at all. It stopped being
  // safe when rows became DRAG SOURCES. Cancelling a mousedown's default on a
  // draggable element is how a drag is cancelled, and on WebKit the `click`
  // that would have followed does not arrive either — so Shift/Mod+click ran
  // this handler and then nothing else, no range was extended, no row toggled,
  // and multi-select was silently dead. A plain click never took this branch,
  // which is exactly the shape the bug was reported in ("multi folder selection
  // using mouse doesn't work anymore").
  //
  // The click-suppression half of that is reported behaviour, not something
  // this codebase can demonstrate: synthesising a modified NATIVE click is
  // outside what the available tooling can do. Which is the other reason the
  // fix is shaped this way — it does not depend on the mechanism being what we
  // think it is. Nothing here cancels a mousedown default any more, so whatever
  // that default does to the click is no longer our business.
  //
  // So the suppression moved off the mousedown default and onto the two places
  // that do not fight the drag:
  //   • `selectstart` on the scroller (registered natively below — React has no
  //     synthetic event for it), which is the browser's own "a selection is
  //     about to begin/extend here" hook and cancels it without touching the
  //     mousedown;
  //   • collapsing any existing range when a modified press lands
  //     (onRowPointerDown), so a range anchored OUTSIDE the listing has nothing
  //     to paint from.
  const collapseNativeSelection = () => {
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

  // The header `⋮` (right end of the MODIFIED column). Everything here is about
  // THIS FOLDER, which is what the crumb bar's own `⋮` used to be for over a
  // listing — that one is gone (Breadcrumb.tsx) and its two unique items moved
  // in below the background menu's set, so there is one place to look instead
  // of a path menu and a right-click each holding half the folder's actions.
  //
  // Discoverability is the whole point of the button: the same items were
  // already a right-click on the background, which nobody finds, and which an
  // empty folder gives you no obvious surface to try.
  const openHeaderMenu = (e: React.MouseEvent<HTMLButtonElement>) => {
    e.preventDefault();
    e.stopPropagation(); // never sorts — the th around it is a sort control
    const r = e.currentTarget.getBoundingClientRect();
    setMenu({
      // Right-ish alignment by hand: ContextMenu only clamps at the VIEWPORT
      // edge, and with a preview pane open this button is nowhere near it, so
      // an unbiased x would hang the popup off to the right of the column.
      // 220 is the menu's own min-width (context-menu.css).
      x: Math.max(4, r.right - 220),
      y: r.bottom + 2,
      items: [
        ...backgroundMenu(),
        "separator",
        {
          label: "Split right",
          icon: <SplitRightIcon size={16} />,
          onClick: () => enterPanel(fsPath, "row"),
        },
        {
          label: "Split down",
          icon: <SplitDownIcon size={16} />,
          onClick: () => enterPanel(fsPath, "col"),
        },
      ],
    });
  };

  // The folder's `⋮`, absolutely positioned against the LAST header cell's
  // right edge (the th is sticky, so it is already the positioned ancestor) —
  // NOT a fourth column: rows have three cells, and a column they don't render
  // breaks their backgrounds. It overlays the header's own padding, which is
  // why .col-mtime reserves room for it (explorer.css) rather than letting it
  // land on the sort arrow.
  // Both handlers stop propagation: the normal header's th sorts on click, and
  // a press that re-sorts the listing under the menu about to open is not what
  // the button says it does. One element, three homes — the mtime th, the
  // search header's Path th, and (labels hidden) the empty folder's strip —
  // because the actions act on the current folder in every one of them.
  //
  // Only where this listing OWNS the bar chrome: the menu replaces the crumb
  // bar's path `⋮`, so it belongs to the same view that dropped it. An
  // embedded or pane-hosted listing never had that menu — and its "Split
  // right" would rewrite the SHELL's URL from inside a nested surface.
  const headerMenuBtn = !ownsBarChrome ? null : (
    <button
      type="button"
      className="listing-head-menu"
      aria-haspopup="menu"
      aria-label="Folder actions"
      title="Folder actions"
      onPointerDown={(e) => e.stopPropagation()}
      onClick={openHeaderMenu}
    >
      <EllipsisIcon />
    </button>
  );

  // --- table body -----------------------------------------------------------

  let body: React.ReactNode;
  // Column headers describe columns of data; over an empty folder they label
  // nothing and just push the "Empty directory" message down (most visible in
  // the preview pane, where NAME/SIZE/MODIFIED sat above one line of text).
  // Set by the empty branch below, read by the <thead> render.
  let emptyDir = false;
  // Every row that spans the table, in the mode it is being rendered for
  // (listing/types columnCount): three columns normally, one while searching.
  const cols = columnCount(searching);
  if (searching) {
    if (validWalk.status === "error") {
      body = (
        <tr>
          <td colSpan={cols} className="status-message error">
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
                /* What this row ACCEPTS, for the pointer drag's hit test
                   (listing/row-drag.ts). Not a drag SOURCE: where a drag may
                   start is decided once, at pointerdown, by the arbiter. */
                data-fs-drop-path={childPath}
                data-fs-drop-dir={entry.is_dir ? "1" : "0"}
                className={
                  "row" +
                  (selectedSet.has(childPath) ? " selected" : "") +
                  // Marker only (no styling of its own): the lead row is what
                  // the scroll-into-view effect tracks.
                  (childPath === selectedPath ? " lead" : "") +
                  (cutSet.has(childPath) ? " cut" : "") +
                  (copiedSet.has(childPath) ? " copied" : "")
                }
                onPointerDown={(e) => onRowPointerDown(e, childPath)}
                onPointerUp={(e) => onRowPointerUp(e, childPath)}
                onDoubleClick={() =>
                  onRowDoubleClick({
                    path: childPath,
                    name: entry.rel.split("/").pop() ?? entry.rel,
                    isDir: entry.is_dir,
                    parentDir: dirname(childPath),
                  })
                }
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
                  {/* Layout only — the span hugs the icon+name so a long name
                      ellipsizes inside it. It is NOT a drag source: a drag
                      starts on an already-selected row and nowhere else
                      (drag-drop's pressStartsDrag). */}
                  <span className="row-handle">
                    <span className="icon">
                      {iconForEntry(
                        entry.rel.split("/").pop() ?? entry.rel,
                        entry.is_dir,
                      )}
                    </span>
                    <span className="search-path">
                      {renderHighlight(entry.rel, positions)}
                    </span>
                  </span>
                  <ClipMark
                    cut={cutSet.has(childPath)}
                    copied={copiedSet.has(childPath)}
                  />
                </td>
                {/* No size/modified cells: a hit's cell holds a whole rel
                    path, and the two fixed-width columns were spending a
                    third of the table on values nobody searches by. */}
              </tr>
            );
          })}
          {cappedAway > 0 && (
            /* No sentinel and no "load more": past the top hundred a fuzzy
               rank stops being useful, so the answer is a better query. The
               count in the search chip carries the real total. */
            <tr>
              <td colSpan={cols} className="status-message">
                {cappedAway.toLocaleString()} more match
                {cappedAway === 1 ? "" : "es"} not shown
              </td>
            </tr>
          )}
        </>
      );
    } else if (scanPending) {
      // The corpus is in hand but this query has not been scored yet (the
      // scan is debounced and sliced — listing/scan-job). An index-backed
      // corpus makes the walk read "ok" instantly, so without this the empty
      // result would render as a confident "No matches" for a moment on
      // every keystroke.
      body = (
        <tr>
          <td colSpan={cols} className="status-message">
            Searching…
          </td>
        </tr>
      );
    } else if (validWalk.status === "ok" || validWalk.status === "streaming") {
      // No matches. Say so honestly: distinguish "still looking" (stream
      // running) and "the walk didn't even cover everything" (truncated) —
      // the old UI showed a bare "No matches" even when the file existed
      // in a region the capped walk never reached.
      // The entries-scanned count belongs to the live fallback WALK, and only
      // that walk moves it. When the index serves the corpus there is no walk
      // at all — the fetch is one request — so the number sat at 0 for the
      // whole (sub-second) window and the row read "still searching (0 entries
      // scanned)" every time. Show the progress only once there is progress to
      // show; before that all we can honestly say is that we are looking.
      const message =
        validWalk.status === "streaming"
          ? validWalk.count > 0
            ? `No matches yet — still searching (${validWalk.count.toLocaleString()} entries scanned)`
            : "Searching…"
          : validWalk.truncated
            ? `No matches in the first ${validWalk.total.toLocaleString()} entries — this folder tree is too large to search fully`
            : "No matches";
      body = (
        <tr>
          <td colSpan={cols} className="status-message">
            {message}
          </td>
        </tr>
      );
    } else {
      body = (
        <tr>
          <td colSpan={cols} className="status-message">
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
        <td colSpan={cols} className="status-message error">
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
          /* See the search-hit row above: what this row ACCEPTS, never where a
             drag may start. */
          data-fs-drop-path={childPath}
          data-fs-drop-dir={entry.is_dir ? "1" : "0"}
          className={
            (entry.ignored ? "row ignored" : "row") +
            (newNames.has(entry.name) ? " row-new" : "") + // brief dir-watch tint
            (selectedSet.has(childPath) ? " selected" : "") +
            (childPath === selectedPath ? " lead" : "") + // scroll-into-view marker
            (cutSet.has(childPath) ? " cut" : "") +
            (copiedSet.has(childPath) ? " copied" : "")
          }
          onPointerDown={(e) => onRowPointerDown(e, childPath)}
          onPointerUp={(e) => onRowPointerUp(e, childPath)}
          onDoubleClick={() =>
            onRowDoubleClick({
              path: childPath,
              name: entry.name,
              isDir: entry.is_dir,
              parentDir: base,
            })
          }
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
            {/* Layout only — see the search-hit row above. Not a drag source. */}
            <span className="row-handle">
              <span className="icon">
                {iconForEntry(entry.name, entry.is_dir)}
              </span>
              {entry.name}
            </span>
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
        <td colSpan={cols} className="status-message">
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
        <td colSpan={cols} className="status-message">
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
  //
  // Two strings per state: a TERSE one to show and the full sentence to say.
  // The chip is pinned inside the input's right edge, so every character it
  // spends is a character the query cannot use — and since the row moved up
  // into the crumb bar (search-slot.ts) it is competing with the path as well.
  // "1,204 matches · 45,110 scanned…" was most of a narrow box. The numbers are
  // the whole message; "matches" and "scanned" are recoverable from context by
  // anyone looking at a list of search results, and stay in the title and the
  // aria-label for anyone who is not.
  const compact = (n: number) =>
    n.toLocaleString(undefined, { notation: "compact", maximumFractionDigits: 1 });

  let searchCount: string | null = null;
  let searchCountFull: string | undefined;
  // The chip's reserved width covers a match count; the scan caveat makes it
  // longer, so the input reserves more while one is running.
  let widePin = false;
  if (searching && validWalk.status === "streaming") {
    // Live progress while the walk streams: match count so far + how much of
    // the tree has been scanned. Updates in place, no layout shift. The scan
    // total is the digit-hungry half and the one nobody reads precisely, so it
    // is the half that goes compact ("45.1K").
    searchCount = `${compact(hits.length)} · ${compact(validWalk.count)}…`;
    searchCountFull = `${hits.length.toLocaleString()} match${hits.length === 1 ? "" : "es"} · ${validWalk.count.toLocaleString()} entries scanned so far`;
  } else if (searching && validWalk.status === "ok" && hits.length > 0) {
    // A truncated walk (server safety cap) means `hits` undercounts the real
    // tree. Signal that without new UI: a "+" on the number plus a tooltip.
    // Terse form for the chip, full sentence for title/aria. Past the display
    // cap the chip has to own up to it — "top 100 of 4.9K+" — because the
    // rendered list stops at the cap while the count keeps reporting the whole
    // ranking. The cap itself stays out of this file (result-cap.ts owns it):
    // `cappedAway` says whether it bit, `visibleHits` says how many rows show.
    const suffix = validWalk.truncated ? "+" : "";
    searchCount =
      cappedAway > 0
        ? `top ${visibleHits.length} of ${compact(hits.length)}${suffix}`
        : `${compact(hits.length)}${suffix}`;
    searchCountFull = resultCountLabel(hits.length, validWalk.truncated);
    if (validWalk.truncated)
      searchCountFull += ` — search covers the first ${validWalk.total.toLocaleString()} entries of this folder tree`;
  }

  // --- index scan caveat ----------------------------------------------------
  // Folded into the status chip rather than added beside it: the chip is
  // absolutely pinned inside the input, so a second element in that row would
  // have to compete with it for the same few pixels on a narrow pane. Both
  // facts are about the same search, and one line says both. Which message
  // appears is a claim about how far the results can be trusted, so it lives
  // in a pure, tested helper (listing/index-caveat).
  const caveat = searching ? indexCaveat(indexScan) : null;
  if (caveat) {
    searchCount = withCaveat(searchCount, caveat);
    searchCountFull = caveat.title;
    widePin = true;
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
          {/* Where the crumb bar lands over a folder (the claim above). It sits
              INSIDE the left column, as its whole header — the search row
              portals up into it — so the bar ends at the divider and the pane
              keeps the whole right-hand column from the top of the window
              down. `display: contents`, so the bar is a flex item of
              .listing-main exactly as it was of #main. */}
          {ownsBarChrome && <div className="listing-crumb-slot" ref={crumbSlotRef} />}
          {/* Embedded (preview pane): no search row — the pane is a glance,
              and the host listing's search/toggle already own that chrome. */}
          {!embedded && inSearchSlot(barSearchSlot,
            /* `searching` (a non-empty query) is what tells the crumb bar to
               stand the crumbs down and give the row its whole width — see
               #breadcrumb:has(.listing-search.searching) in explorer.css.
               Nothing to hand upward: the row is portaled INTO the bar, so a
               class on the row is already inside the bar's subtree. */
            <div
              ref={searchRowRef}
              className={
                "listing-search" +
                (searching ? " searching" : "") +
                (tightBar && !searching && !pinnedOpen ? " iconized" : "")
              }
            >
              {/* Tight bar (see the measurement above): the resting box is
                  folded away by .iconized and this magnifier stands in for it.
                  Clicking pins the box open and focuses it; blurring it still
                  empty hands the strip back to the path. */}
              {tightBar && !searching && !pinnedOpen && (
                <button
                  type="button"
                  className="bar-ctl listing-search-open"
                  aria-label="Search this folder"
                  title="Search"
                  onClick={() => setPinnedOpen(true)}
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                    <circle cx="11" cy="11" r="7" />
                    <line x1="16.5" y1="16.5" x2="21" y2="21" />
                  </svg>
                </button>
              )}
              {/* The box wraps input + pinned chips so the pane toggle can sit to
            their right without disturbing the chips' inside-the-input pin.
            `has-pin` says a chip is actually pinned right now, so the input
            reserves room for one only then — the reservation is wide, and
            idle it was dead space that clipped the placeholder in a narrow
            window. */}
              <div
                className={
                  "listing-search-box" +
                  (hasPin ? " has-pin" : "") +
                  (widePin ? " wide-pin" : "")
                }
              >
                <input
                  ref={searchInputRef}
                  type="search"
                  className="listing-search-input"
                  // Just "Search…": the row shares the crumb bar now, and the
                  // resting box is deliberately small (it grows to the whole
                  // strip on the first keystroke), so the placeholder has to
                  // fit that box rather than set its width. "Start typing to
                  // search" was instructions for a control that needs none.
                  placeholder="Search…"
                  value={query}
                  // Focus pins the box open, whatever routed it here — the
                  // magnifier click, or type-to-search landing focus on the
                  // zero-width folded input (useListingSelection's
                  // printable-key branch; the .iconized CSS keeps the input
                  // focusable for exactly this). The pin also holds while a
                  // focused user deletes their query — the box must not fold
                  // away under the caret.
                  onFocus={() => {
                    setPinnedOpen(true);
                    prefetchWalk();
                  }}
                  // A pinned-open box that blurs still empty folds back to the
                  // magnifier (the pin exists only to be typed into); with a
                  // query it stays — .searching owns the strip from there.
                  onBlur={(e) => {
                    if (!e.currentTarget.value) setPinnedOpen(false);
                  }}
                  onChange={(e) => setQuery(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Escape") {
                      e.preventDefault();
                      setQuery("");
                      // Explicit, not left to onBlur: the blur below fires
                      // before React writes the cleared value into the DOM,
                      // so the handler would read the pre-Esc query and keep
                      // the pin.
                      setPinnedOpen(false);
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
                    title={searchCountFull}
                    aria-label={searchCountFull}
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
              {/* THE PANE'S OPENER, and the second half of one affordance: the
                  closing chevron is a control ON the pane's own header, at the
                  seam it collapses toward (SideChrome, where the split is written
                  down), so this button is on screen only while the pane is SHUT.

                  It is not the old pane toggle coming back. That one was an
                  on/off for a bit the layout could answer itself, and it went
                  when the split became purely a measurement of the container's
                  width (listing/pane.ts) — `pane.on` below is still that
                  measurement, and this button does not exist when it says no. It
                  is a mode control: it says WHICH of the pane's three would
                  return, wearing that mode's own icon, which is a thing the
                  layout cannot answer.

                  Here rather than in the crumb bar because over a folder THIS ROW
                  is the bar (it portals into it — search-slot.ts), and this is the
                  folder's own chrome, beside the folder's own search box. */}
              {pane.on && !sideState.open && (
                <SideToggleButton
                  what={modeTitle(paneSide)}
                  icon={paneSideIcon(paneSide, sideEntries)}
                  onClick={openSide}
                />
              )}
              {/* The path `···` is not here any more: it rides the crumb strip
                  now (Breadcrumb.tsx), immediately right of the folder name it
                  acts on, which is one home instead of this row's and the
                  file view's. */}
            </div>
          )}
          <div
            ref={scrollRef}
            /* Dimmed both when the deferred render lags a keystroke and while
             held (pre-refresh) results stand in for a re-running walk. */
            className={"listing-scroll" + (isStale || showingHeld ? " listing-stale" : "")}
            /* The background means THIS FOLDER: "move these here". It lights up
               (.drop-into, painted by row-drag.ts) only when that would actually
               move something — dropping rows into the folder they already live
               in is a no-op, which dropIsValid already spells out. */
            data-fs-drop-path={normDir(base)}
            data-fs-drop-dir="1"
            /* THE PRESS ARBITER, and the CAPTURE phase is load-bearing: it runs
               before the row's own pointerdown, so the selection it snapshots is
               the one from before this press. A press on an already-selected row
               drags the selection; anywhere else sweeps; a press that barely
               moves is still the click it always was. */
            onPointerDownCapture={onListingPointerDownCapture}
            /* No onClick here: clicking the empty area below the rows does
               NOT deselect. Finder's rule, and it cost more than it bought
               once the preview pane arrived — a stray click anywhere in the
               whitespace of a short listing blanked the pane and threw away
               the row the user was reading. Escape still clears (the
               deliberate gesture); the background is just background. */
            onContextMenu={openBackgroundMenu}
          >
            <table className="listing-table">
              {/* Over an empty folder the column LABELS hide (visibility, see
                  .listing-head-empty) but the strip stays: the folder `⋮`
                  lives on it, and an empty folder is where that menu matters
                  most. */}
              <thead className={emptyDir ? "listing-head-empty" : undefined}>
                <tr>
                  {searching ? (
                    // One column, and NOT a sort control. Results are in
                    // relevance (fuzzy-rank) order, full stop: the hit set is
                    // capped and, while the walk streams, partial — ordering
                    // that by name or date presents it as an answer it isn't,
                    // and the search box already says the coverage is
                    // approximate (listing/index-caveat).
                    // The folder `⋮` stays through a search: its actions act on
                    // the CURRENT folder either way, and search replacing the
                    // one header that carried it would make the control come
                    // and go with the query.
                    <th className="col-name">
                      Path
                      {headerMenuBtn}
                    </th>
                  ) : (
                    (Object.entries(SORT_KEYS) as [SortKey, string][]).map(
                      ([key, label]) => (
                        <th
                          key={key}
                          className={
                            `sortable col-${key}` +
                            (key === sort ? " sorted" : "")
                          }
                          onClick={() => setSort(key)}
                        >
                          {/* Wrapped so the empty-folder state can hide the
                              LABEL without unmounting the strip — the `⋮` on
                              this row must survive it (explorer.css,
                              .listing-head-empty). */}
                          <span className="col-label">{label}</span>
                          {/* One glyph that ROTATES for desc (see .sort-arrow):
                          swapping ▲ for ▼ replaced the element, so the change
                          could only ever pop. */}
                          {key === sort && (
                            <span
                              className={
                                "sort-arrow" + (order === "desc" ? " desc" : "")
                              }
                            >
                              ▲
                            </span>
                          )}
                          {key === "mtime" && headerMenuBtn}
                        </th>
                      ),
                    )
                  )}
                </tr>
              </thead>
              <tbody>{body}</tbody>
            </table>
          </div>
        </div>
        {paneOpen && (
          <>
            <div
              className="listing-divider"
              onPointerDown={onDividerPointerDown}
              role="separator"
              aria-orientation="vertical"
            />
            <div
              className="listing-pane-slot"
              // A PERCENTAGE, not a pixel width: the split is a fraction of
              // this container (listing/pane.ts), so a window resize keeps the
              // proportion the user dragged instead of leaving the pane at one
              // window's arithmetic — and, until it IS dragged, steps between
              // 30/50/70% as the container crosses the width breakpoints. The
              // pixel floors are the slot's / the list's CSS min-widths.
              style={{ flexBasis: `${pane.frac * 100}%` }}
            >
              {/* Keyed on WHAT THE PANE IS ABOUT (pane-side's paneKey), which is
                  the previewed row for two of the three modes and the FOLDER for
                  Git — see there. Keying on the row is what stops a stale iframe
                  lingering a frame while the new row's stat/list resolves; keying
                  Git on the folder instead is what stops arrow-keying down the
                  listing reloading a `git status` per keystroke.
                  Nothing selected → the pane previews THIS folder itself (self:
                  its template or lone app — never its listing, which is already on
                  the left). */}
              <ListingPreviewPane
                key={paneKey(
                  paneSide,
                  fsPath,
                  sel.paths.length === 1 && leadRow ? leadRow.path : null,
                  sel.paths.length
                )}
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
                folder={fsPath}
                side={paneSide}
                sideEntries={sideEntries}
                onSelectSide={selectSide}
                onClose={closeSide}
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
