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
import { IS_PANEL_PANE, IS_SNAPSHOT, navigate, replaceSearch } from "@platform/lib/router";
import { dirname, normDir } from "@apps/explorer/lib/fs-actions";
import { acquireOverlay, releaseOverlay } from "@platform/lib/ui-overlay";
import { isMod } from "@platform/lib/platform";
import { formatSize, formatMtime, formatMtimeFull } from "@platform/lib/format";
import { iconForEntry } from "@platform/ui/FileIcons";
import { getViewState, setViewState } from "@platform/lib/viewstate";
import { useFlip, FLIP_KEY_ATTR } from "@platform/lib/flip";
import { useClipboard } from "@apps/explorer/lib/fs-clipboard";
import ContextMenu from "@platform/ui/ContextMenu";
import { EllipsisIcon } from "@apps/explorer/BarMenu";
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
  type PaneSideChoice,
  type PaneSideState,
} from "@apps/explorer/listing/pane-side";
import { useDirMode } from "@apps/explorer/lib/dir-mode";
import { SideToggleButton, paneSideIcon } from "@apps/explorer/SideChrome";
import { modeTitle } from "@platform/lib/mode-name";
import { passedDragSlop } from "@apps/explorer/listing/marquee";
import {
  INITIAL_SEARCH_SELECT,
  nextSearchSelection,
  rowPressAction,
  type RowPressAction,
} from "@apps/explorer/listing/selection";
import { useRowDrag } from "@apps/explorer/listing/useRowDrag";
import { useMarquee } from "@apps/explorer/listing/useMarquee";
import { useSettledLead } from "@apps/explorer/listing/useSettledLead";
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

// Flips the tight-bar measurement is allowed at one bar width before it holds.
// The reasoning is at `tightFlipRef` and at the layout effect it guards.
const FLIP_BUDGET = 2;

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
    behind,
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
  // disabled at the hook, however wide the embedded listing gets. **These three
  // flags are now the WHOLE of whether there is a pane** — `pane.on` is exactly
  // `paneEnabled` since D282 deleted the width gate, so "is this a Listing that has
  // a pane" is a question about the SURFACE and never about pixels.
  //
  // A FROZEN-TREE listing is the second no-nesting case, and `embedded` cannot
  // see it: the browsable snapshot (the `browse` framing of the removed timeline
  // mode, PT-14) is a whole shell loaded at `/explorer/embed/<tree>?snapshot=1`,
  // so its Listing is the page's OWN top-level one — `embedded=false` — inside
  // the framing view's preview column, where it grew a preview pane INSIDE a
  // preview pane. `?preview=false` used to stop it and was dropped with the toggle
  // it belonged to, on the reasoning that the width decides — true for a listing
  // that owns its window, false for one handed a column by a framer. *That
  // reasoning is doubly dead now: with the width gate deleted (D282) the framed
  // listing would grow a pane at ANY column width, so this flag is not a
  // refinement of a measurement but the whole answer.*
  //
  // `snapshot=1` and not a second param of its own: the framing flag has
  // exactly one producer, and that producer is a template framing this listing
  // in its own column. A flag that could only ever be written beside another
  // one is the "three places to agree about one bit" the pane's own history
  // (pane.ts) is a warning about.
  //
  // A PANEL PANE is the third, and it is the same shape of blind spot as the
  // snapshot: a pane is a whole shell at `/explorer/embed/<path>`, so its
  // Listing is that frame's own top-level one — `embedded=false`, `barChrome`
  // true, everything about it says "I own this window". What it does not own is
  // the layout: the user split it, so a split-right of a folder grew two
  // half-width listings each with their own preview. Four columns where the user
  // asked for two — and no width test could ever have objected, because the width
  // was genuinely there. IS_PANEL_PANE is the host-side question a measurement
  // cannot answer (see router.ts, including why `IS_EMBED` — which is also
  // every TAB, where the pane is right and stays — is the wrong flag here).
  //
  // Switching it off HERE is the whole feature: `pane.on` IS this predicate
  // (D282 left nothing else in it), so one flag takes the slot, the divider, the
  // closing chevron on the pane's header, the reopening SideToggleButton in the
  // search row and the two `useDirMode` companion probes with it. Nothing about the
  // ROWS changes: a pane's listing still selects, arrow-keys, and opens on
  // double-click/Enter —
  // opening a file in a pane replaces that pane's document, which is the point.
  const paneEnabled = !embedded && !IS_SNAPSHOT && !IS_PANEL_PANE;
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
  // A folder outside a repository cannot SHOW Git, and one on a mount can show
  // neither companion (each gate refuses a mount-backed path) — at which point the
  // pane's switcher lists them disabled, saying why (pane-side's paneSideMenu),
  // rather than shrinking to a Preview-only pill and hiding itself.
  const folderClaude = useDirMode(paneEnabled ? fsPath : null, "claude");
  const folderGit = useDirMode(paneEnabled ? fsPath : null, "git");
  // While the probe is in flight the entries are PLACEHOLDERS with no template
  // path (lib/dir-mode), which would build a `path=null` iframe URL — so a
  // pending companion is not SELECTABLE yet. Unlike the file sidebar there is
  // nothing to protect by treating it as selectable early: the folder's `_side` is
  // never reconciled away (pane-side's activePaneSide leaves an unavailable
  // request in the URL on purpose), so a `?_side=git` deep link survives the wait
  // and lands the moment the verdict does.
  //
  // The extra fields ride along for the SWITCHER alone, which has to say more
  // than "offered": the flags tell "we don't know yet" (spinner) from "not here"
  // (the disabled reason), and the bindings are where a disabled row gets the
  // mode's REAL icon — lib/dir-mode keeps a denied entry for exactly that, so the
  // Git row is the Git glyph dimmed instead of a boxed "G". Nothing else reads
  // either; what the pane may BE is still `claude`/`git` alone.
  const sideEntries = {
    claude: folderClaude.pending ? null : folderClaude.entry,
    git: folderGit.pending ? null : folderGit.entry,
    claudePending: folderClaude.pending,
    gitPending: folderGit.pending,
    claudeBound: folderClaude.bound,
    gitBound: folderGit.bound,
  };
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

  const clipboard = useClipboard();

  // Search input, so a keystroke anywhere in the listing can focus it.
  const searchInputRef = useRef<HTMLInputElement>(null);
  // --- resting search box folds to a magnifier on a tight bar ---------------
  // The bar's yield order (crumbs shrink → box shrinks, explorer.css) bottoms
  // out with the PATH still ellipsized on a narrow middle column, while the
  // idle box holds ~100px of placeholder. Past that point the box becomes a
  // 28px icon and the path gets the strip back. `tightBar` is the measured
  // fact; `pinnedOpen` is the user overriding it (clicked the icon, or focused
  // the box — the box stays until it blurs empty), and it now also renders
  // `.expanded`, so the override is the SAME full-strip box a query gets
  // rather than a second, narrower open state. A non-empty query outranks
  // both: `.searching` stands the crumbs down and takes the whole strip too.
  const [tightBar, setTightBar] = useState(false);
  const [pinnedOpen, setPinnedOpen] = useState(false);
  // How many times the tight-bar measurement may flip at one bar width before
  // it stops arguing with itself — the convergence guarantee for the layout
  // effect below, which has no dependency array and so re-enters on every
  // commit it causes. Two is enough for every honest case: the first flip is
  // the decision, the second absorbs a settling relayout (a scrollbar, a
  // shed column) that legitimately reverses it. A third flip at an unchanged
  // width is not new information, it is the bistable case, and past 50 of
  // those React unmounts the whole tree with #185. Lives in a ref, not state,
  // because spending budget must not itself schedule a render.
  const tightFlipRef = useRef({ barW: -1, flips: 0 });
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
  // Two thresholds, meant to be far enough apart that the flip cannot
  // oscillate:
  //   • fold: the crumbs are ellipsized (scrollWidth past clientWidth) even
  //     after the CSS yield order has bottomed out — the box is the only
  //     slack left to give.
  //   • unfold: the whole path is showing AND the bar has THE WHOLE RESTING BOX
  //     genuinely free — room it can take without re-truncating anything (it
  //     only has to give back the ~30px the magnifier standing in for it
  //     occupies, so the box's own width is the threshold plus that margin).
  //     Free space is summed from the bar's visible children, not read off the
  //     crumbs: they are flex-grow 0 in slot mode, so their clientWidth hugs
  //     their content and never reports the strip's slack.
  // The resting width is read from the box (--resting-width, explorer.css)
  // rather than hardcoded, because it is NOT one number: a box with a chip
  // pinned in it (`.has-pin` — in practice the multi-selection readout) is
  // 260px, not 150px. A fixed 150 was one half of the bug that blanked the
  // whole view the moment a second row was selected: 150px of slack was enough
  // to unfold into and nowhere near enough to hold a 260px box, so the crumbs
  // re-ellipsized, the bar folded, the freed slack cleared 150 again — a
  // fold/unfold flip on every commit until React gave up with "maximum update
  // depth exceeded" (#185) and unmounted the tree, a BLANK PAGE. Reading the
  // threshold off the element is also what keeps it and the width from
  // drifting apart the next time either moves.
  //
  // WHICH STATE the measurement is about is read off the DOM (`.iconized`) and
  // NOT out of React state, and the setState below is passed a plain boolean
  // rather than an updater function. Both halves of that are load-bearing: every
  // width here is measured from one layout, so "is the box folded right now" has
  // to be answered by that same layout. An updater function is evaluated by
  // React on ITS schedule — eagerly when the setter is called, to test whether
  // the update can bail out, and again while rendering — so an updater that
  // measures the DOM answers a different question each time it runs and the two
  // answers disagree. That disagreement was the other half of the blank-screen
  // crash: `folded` arrived describing the commit that had not painted yet while
  // the widths described the one on screen, the fold decided on the mismatched
  // pair, and the flip never settled.
  //
  // FLIP_BUDGET stays as the backstop underneath both of those. The thresholds
  // are meant to be honest hysteresis now, but they are still two DOM
  // measurements taken in two different layouts, and any future width whose
  // fold delta lands between them makes the flip bistable again. Once a bar
  // width has spent its budget the measurement holds whatever it is showing
  // until the width actually changes: a width where the thresholds agree
  // converges in one flip and never touches the budget; a width where they
  // contradict settles on a legible state instead of taking the page down.
  // Keyed on the bar's width because that is what a contradiction is a
  // property of — a real resize is new information and earns a fresh budget.
  //
  // No dependency array: crumbs content changes with navigation but their
  // clientWidth may not, so a ResizeObserver alone misses scrollWidth-only
  // changes; re-measuring on every render is cheap. Skipped while the user is
  // in the box — measuring a strip the crumbs have stood down from
  // (.searching hides them) reads zeros.
  useLayoutEffect(() => {
    if (embedded || searching || pinnedOpen) return;
    const row = searchRowRef.current;
    if (!row) return;
    const bar = row.closest("#breadcrumb");
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
    // How much strip unfolding would ask for: whatever CSS says the resting box
    // is right now. The fallback is the idle width, for the frame before the
    // stylesheet is attached (a missing property parses to NaN, which would
    // make every comparison false and unfold unconditionally).
    const restingWidth = () => {
      const box = row.querySelector(".listing-search-box");
      const w = box
        ? parseFloat(getComputedStyle(box).getPropertyValue("--resting-width"))
        : NaN;
      return Number.isFinite(w) ? w : 150;
    };
    const measure = () => {
      // Fresh width, fresh budget (see FLIP_BUDGET above).
      const barW = bar.clientWidth;
      const budget = tightFlipRef.current;
      if (budget.barW !== barW) {
        budget.barW = barW;
        budget.flips = 0;
      }
      // The layout on screen, and the state that layout IS — one pair, read
      // together, and decided OUT HERE rather than inside a setState updater
      // (see the comment above on why neither half may come from React: the
      // decision reads the DOM and spends the budget, and an updater must
      // stay pure — React is free to call it more than once for one update).
      const folded = row.classList.contains("iconized");
      const ellipsized = crumbs.scrollWidth > crumbs.clientWidth + 1;
      const next = folded ? ellipsized || freeInBar() < restingWidth() : ellipsized;
      if (next === folded) return;
      if (budget.flips >= FLIP_BUDGET) return; // bistable at this width — hold
      budget.flips += 1;
      setTightBar(next);
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
    barMenu,
  } = useFileOps({ base, clipboard, refetch, pendingSelectRef, ownsBar: ownsBarChrome });

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

  // OPENING A FOLDER SELECTS NOTHING (FS-16, D278). There is no folder
  // auto-select here and there is deliberately no code for one: a freshly opened
  // folder has an empty selection, so its pane sits on its self target — showing the
  // chat about the folder, since a folder is not a thing the pane previews (FS-11,
  // D284) — until the user picks a row.
  //
  // What used to be here was a one-shot effect that walked the settled rows for
  // the first page, else the first row, and selected it — so the pane always had
  // something in it. It went because the guess is a real action taken on the
  // user's behalf: it highlights a row they did not choose, mounts a /render
  // iframe (and a template's Python) for a file they may never look at, and
  // makes the keyboard's target and every row-scoped action — delete, rename,
  // the pane's expand — point at whatever the sort put first. An empty pane asks
  // for one click; a wrong selection has to be noticed and undone.
  //
  // Nothing else about the selection changed. A `?sel=` on the URL is still
  // seeded at mount and a click in the pre-stat provisional scaffold still
  // carries across the swap (both in useListingSelection: pathFromSelParam and
  // recallSelection), because those are the user's own claims rather than the
  // app's guess. And SEARCH still lands on its top hit, right below — a query is
  // itself a request to look at something, which is exactly what opening a
  // folder is not.

  // Search results land on their TOP HIT, so Enter and the pane act on the
  // best match without the user having to reach for it first.
  //
  // This is the LAST auto-selection in the listing (the folder one is gone,
  // above) and it is a repeated one, not a shot: results re-rank on every
  // keystroke, every stream flush and every slice the scan publishes. The
  // decision (searchAutoSelectPath) owns what to select; this owns only two
  // things.
  //
  // WHEN to ask. Not while embedded (the pane's own `_listing` has no pane to
  // fill) and not provisional — a scaffold's selection would be torn down and
  // re-made by the swap to the resolved listing a beat later. It does NOT wait
  // for `pane.on`: a selected top hit is worth having for Enter and the arrow
  // keys whether or not the window is wide enough to preview it.
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

  // THE ROW THE PANE IS ABOUT, which is the lead ONCE IT HAS SETTLED (D281's cost
  // fix — the rule, and why a pane mount has to be earned, are on pane-settle.ts).
  // Every mount is an iframe load, and the `claude` side's iframe spawns `agent.py`
  // through /api/run before it draws, so the pane must not chase a held arrow key
  // down the listing. A move from rest still lands at once; only the rows passed
  // THROUGH are skipped.
  //
  // The whole pane reads this and not `sel.lead`: the row it renders, the mode key
  // that remounts it, and the folder/file question behind the pill. Reading the
  // live lead for any of them would put the per-keystroke mount straight back.
  // The LEAD is settled unconditionally — not `paths.length === 1 ? lead : null`,
  // which mixed a live count into a settled value and made the pane flash its
  // "Select a file to preview." hint with a row plainly selected: collapsing a
  // 2-row selection onto a third row moved the count to 1 while the settled lead
  // was still catching up from the multi-selection's `null`, so for one settle
  // window there was a count of one and no row to show for it. Settling the lead
  // alone means the pane TRAILS onto the previous row instead, which is what a
  // debounce means and what arrowing already looks like.
  const settledLead = useSettledLead(sel.lead);
  // …and the pane's STATE stays live, because none of its other states mount
  // anything: nothing selected is the self target, two or more is the count
  // placeholder, and both must land the instant the selection does. Only the
  // single-row case is expensive, and only it is settled.
  const paneRow =
    sel.paths.length === 1 && settledLead ? rowCtxByPath.get(settledLead) : undefined;

  // WHICH of the pane's three modes it is on. Resolved here, below the selection,
  // because no FOLDER SUBJECT has a `preview` side at all (pane-side's
  // paneSideList, D281/D284) and so lands on the chat about it — a folder is not a
  // thing this pane previews.
  //
  // **A FOLDER SUBJECT IS TWO STATES**: a selected directory, and NOTHING SELECTED,
  // where the subject is this folder itself. D281 did only the first, which left the
  // state every folder OPENS into (FS-16) reading "Preview" over a "Select a file to
  // preview." hint — the more visible half of the same bug, and what the owner
  // reported next. The self target keeps that hint only as the neither-companion
  // fallback now (ListingPreviewPane's self branch).
  //
  // **The subject no longer enters into it at all** (D285): `preview` is not on offer
  // for any row type, so there is nothing left to ask about the subject and
  // `paneSideList` takes no flag. What it answers is what the FOLDER offers.
  // The open folder's entry page, or null when it has none — a lookup over the rows
  // this listing already fetched, so it costs no request and re-answers itself on a
  // dir-watch refresh like any other derived row value. Literally `index.html`
  // (case-insensitive, and a FILE — a directory of that name is a real shape), which
  // is the narrow reading of the owner's "the open button simply opens the
  // index.html" and the first clause of the server's own entry rule.
  const appEntryPath = useMemo(() => {
    const hit = sortedEntries.find(
      (e) => !e.is_dir && e.name.toLowerCase() === "index.html",
    );
    return hit ? base + "/" + hit.name : null;
  }, [sortedEntries, base]);

  const paneSides = paneSideList(sideEntries);
  // UNDECIDED — a folder row whose companion probes have not answered (pane-side's
  // paneSideList returns an empty list, and only for that). The pane holds a
  // skeleton: resolving a side here would put the pill on `preview` while the row's
  // own `claude` default rendered a chat under it, and would then remount — and
  // respawn `agent.py` — when the probe landed.
  const paneUndecided = paneSides.length === 0;
  const paneSide = activePaneSide(paneSides, sideState.mode);

  // Picking the mode that is ALREADY first on offer records NO choice (`mode: null`),
  // so the leading companion keeps the clean URL (PT-9, D285): a click on Claude
  // where Claude is what the pane is already showing must not grow `_side=claude` on
  // every shared listing link. Only a second, deliberate choice is written down.
  //
  // Defined HERE, below `paneSides`, and not up with the other `_side` writers: it
  // reads that list, and a closure over a `const` declared later in the same body is
  // a temporal-dead-zone trap waiting for the first caller that runs during render.
  const selectSide = (mode: PaneSideChoice) =>
    setSide({ open: true, mode: mode === paneSides[0] ? null : mode });

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
  const pressRef = useRef<{
    path: string;
    x: number;
    y: number;
    action: RowPressAction;
  } | null>(null);

  const onRowPointerDown = (e: React.PointerEvent, path: string) => {
    if (e.button !== 0) return;
    const action = rowPressAction({
      mod: isMod(e),
      shift: e.shiftKey,
      inMultiSelection: selectedSet.has(path) && sel.paths.length > 1,
    });
    // Recorded for EVERY press, not only the deferred one: the release measures
    // how far the pointer travelled, and in an EMBEDDED listing it also OPENS —
    // which it may do only for a press this handler read as plain. The decided
    // action rides along rather than the raw modifiers, so the release can never
    // disagree with the press about which of the four gestures this was.
    pressRef.current = { path, x: e.clientX, y: e.clientY, action };
    if (action === "defer") return;
    if (action === "select") {
      selectOnly(path);
      return;
    }
    collapseNativeSelection();
    if (action === "extend") extendTo(path);
    else toggleSelected(path);
  };

  // SINGLE CLICK OPENS — in an EMBEDDED listing only, i.e. in the preview
  // pane's `_listing` mode (ListingPreviewPane). That listing is not a place to
  // build a selection: it is a look INSIDE the folder the middle panel has
  // selected, and the one thing to do with a row there is go to it. The middle
  // panel keeps the unified select-on-press / open-on-double-click model, which
  // is why the gate is `embedded` and not the pane's presence (see the note on
  // onRowDoubleClick, and listing/selection's source test).
  //
  // Only a PLAIN press opens. Shift and Mod still mean range and toggle even
  // here, so the pane's own multi-selection (its context menu, its file ops)
  // stays reachable; those two actions are exactly what this excludes.
  const openOnRelease = (action: RowPressAction) =>
    embedded && (action === "select" || action === "defer");

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
  //
  // The release is also where an EMBEDDED listing OPENS on a single click
  // (openOnRelease above). Both halves want the same two facts — same row, press
  // stayed still — so they share the one handler and the one slop test.
  const onRowPointerUp = (e: React.PointerEvent, path: string) => {
    const press = pressRef.current;
    pressRef.current = null;
    if (!press || press.path !== path) return;
    if (passedDragSlop({ x: press.x, y: press.y }, { x: e.clientX, y: e.clientY })) return;
    if (press.action === "defer") selectOnly(path);
    if (openOnRelease(press.action)) {
      const row = rowCtxByPath.get(path);
      if (row) navigate(row.path, { isDir: row.isDir });
    }
  };

  // Double-click OPENS. Unconditionally: the same gesture in the same folder
  // has to mean the same thing whether or not the window happens to be wide
  // enough for the preview pane (listing/selection documents the model). Enter
  // opens the same target from the keyboard.
  // No single/double-click delay timer: the first click of a double-click
  // selects, which is harmless — any pane fetch it starts is superseded or
  // unmounted by the navigation the second click triggers. In an EMBEDDED
  // listing that first click has already OPENED (openOnRelease), so this rarely
  // gets to run there; it stays wired anyway, because a gesture that opens must
  // not become a no-op in the one surface whose release beat it to it.
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

  // A press on the empty background (not a row) clears the selection. This is
  // a POINTERDOWN, not a click, and that is load-bearing: the marquee captures
  // the pointer on row presses, and capture retargets the pointerup — so the
  // browser computes the follow-up click's target as the SCROLLER for a press
  // that plainly landed on a row. A click handler here read those as
  // background clicks and un-selected every row the moment it was selected
  // (and ate double-click-to-open with it). The pointerdown still carries the
  // press's true target. Modified presses pass through untouched: a
  // Shift/Cmd sweep from the background unions with the selection it started
  // over (useMarquee snapshots `base` in the capture phase, before this runs —
  // and a bare sweep replaces the selection anyway, so clearing first changes
  // nothing for it).
  const onBackgroundPointerDown = (e: React.PointerEvent) => {
    if (e.button !== 0) return;
    if (e.shiftKey || e.metaKey || e.ctrlKey) return;
    const target = e.target as HTMLElement;
    if (
      target !== scrollRef.current &&
      target.tagName !== "TBODY" &&
      target.tagName !== "TABLE"
    ) {
      return;
    }
    // A scrollbar press also targets the scroller; scrolling a long listing
    // must not throw the selection away. The gutters live between the client
    // box and the border box, so a press past clientWidth/clientHeight is on
    // a scrollbar, not the background.
    if (target === scrollRef.current) {
      const { offsetX, offsetY } = e.nativeEvent;
      if (offsetX >= target.clientWidth || offsetY >= target.clientHeight) return;
    }
    selectPaths([]);
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
  //
  // `barMenu()` (useFileOps -> lib/bar-menus) is the list, not an array built
  // here: a right-click anywhere on the crumb bar opens the same one, and two
  // copies of "the folder's actions plus the splits" is how the two surfaces
  // would end up disagreeing about what the folder can do.
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
      items: barMenu(),
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
  const caveat = searching ? indexCaveat(indexScan, behind) : null;
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
                // `expanded` is the FOCUS half of the same geometry `.searching`
                // owns: a box being typed into gets the whole strip, and it
                // should not have to wait for the first keystroke to get it.
                // Two classes rather than one because neither implies the other
                // — a query can outlive the focus that entered it, and a pinned
                // box is usually still empty — and the strip-wide rules in
                // explorer.css name both.
                (pinnedOpen ? " expanded" : "") +
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
                {/* The same magnifier the fold stands in with, now inside the
                    field's left edge — so folding and unfolding is one glyph
                    moving rather than two different marks, and an expanded box
                    still says what it is once the placeholder is typed over.
                    Absolutely positioned and click-through (explorer.css): it
                    costs the box no layout, leaves the chips pinned at the
                    right edge alone, and a press on it lands on the input
                    underneath, which is the focus that expands the box. */}
                <span className="listing-search-glyph" aria-hidden="true">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <circle cx="11" cy="11" r="7" />
                    <line x1="16.5" y1="16.5" x2="21" y2="21" />
                  </svg>
                </span>
                <input
                  ref={searchInputRef}
                  type="search"
                  className="listing-search-input"
                  // Just "Search…": the row shares the crumb bar now, and the
                  // resting box is deliberately small (focus hands it the whole
                  // strip), so the placeholder has to fit that box rather than
                  // set its width. "Start typing to search" was instructions
                  // for a control that needs none.
                  placeholder="Search…"
                  value={query}
                  // Focus pins the box open — and open means the whole strip
                  // (`.expanded` above), because a box being typed into is what
                  // the bar is for. Whatever routed the focus here — a click in
                  // the field, the magnifier click, or type-to-search landing on
                  // the zero-width folded input (useListingSelection's
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
                  on/off for a bit the layout could answer itself, and it went when
                  the split became a measurement of the container's width — a
                  measurement that is itself gone now (D282): `pane.on` below is
                  just "this Listing has a pane", and this button does not exist
                  where it says no. It is a mode control: it says WHICH of the
                  pane's three would return, wearing that mode's own icon.

                  It also carries more weight than it did. With no width gate, a
                  NARROW window shows the pane like any other, so `_side=off` — and
                  this button back from it — is the only way to give a cramped
                  listing the whole column. That is the trade the flat 30% buys.

                  Here rather than in the crumb bar because over a folder THIS ROW
                  is the bar (it portals into it — search-slot.ts), and this is the
                  folder's own chrome, beside the folder's own search box. */}
              {/* OPEN APP — this folder's own `index.html`, opened as the file it is.
                  Shown only when the folder HAS one, hidden otherwise: a button that
                  cannot act is noise, and unlike the pane's companions this is not a
                  mode the user is owed a reason for.

                  The entries are already in hand (`sortedEntries`, the listing this
                  bar sits on), so the question is a lookup and costs no request. The
                  SELECTED-ROW case — a folder row that itself contains an index.html
                  — is deliberately NOT here: a row's children are not in hand, so it
                  needs a per-selection `/api/fs/list`, and nothing already on the
                  wire answers it (the stat's `templates` is the registry's mode list,
                  and lib/dir-mode resolves the FOLDER's own companion entries;
                  neither says anything about a child's children). D280 deleted the
                  resolution that used to do it. If it comes back it hangs off the
                  SETTLED lead (useSettledLead) and off nothing else, or an arrow-key
                  walk down a listing of folders is one request per row.

                  `navigate(path, { isDir: false })` is the row's own double-click
                  path (onRowDoubleClick), reused rather than reinvented: no new view
                  and no `_mode`, just the file. */}
              {appEntryPath && (
                <button
                  type="button"
                  className="bar-ctl"
                  title={"Open " + appEntryPath.slice(appEntryPath.lastIndexOf("/") + 1)}
                  onClick={() => navigate(appEntryPath, { isDir: false })}
                >
                  Open app
                </button>
              )}
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
            /* Two different dims, two different claims. `listing-stale` means
               an answer is on its way (the deferred render lags a keystroke,
               the scan is mid-flight, or held results stand in for a walk that
               is re-running). `listing-behind` means the opposite: no answer is
               coming, these results are a generation old and staying that way
               until a boundary (listing/revalidate). The second can last the
               whole session, so it is deliberately the lighter of the two —
               it has to be legible to read under, not merely noticeable. */
            className={
              "listing-scroll" +
              (isStale || showingHeld ? " listing-stale" : "") +
              (behind ? " listing-behind" : "")
            }
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
            /* Bubble phase, so the marquee's capture snapshot above runs
               first. Deselecting on the CLICK instead is a trap — see
               onBackgroundPointerDown. */
            onPointerDown={onBackgroundPointerDown}
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
                          onClick={() => {
                            setSort(key);
                            selectPaths([]);
                          }}
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
              // proportion instead of leaving the pane at one window's
              // arithmetic. Until it is dragged that fraction is the companion
              // share — 30%, or 50% in a container of 1000px or less (D283), the
              // same rule a file's sidebar reads. The pixel floors are the slot's
              // / the list's CSS min-widths, and under ~440px they are what the
              // pane actually gets: half of anything narrower is below the 220px
              // floor, so the two shares paint identically down there.
              style={{ flexBasis: `${pane.frac * 100}%` }}
            >
              {/* Keyed on WHAT THE PANE IS ABOUT (pane-side's paneKey), which is
                  the previewed row for two of the three modes and the FOLDER for
                  Git — see there. Keying on the row is what stops a stale iframe
                  lingering a frame while the new row's stat/list resolves; keying
                  Git on the folder instead is what stops arrow-keying down the
                  listing reloading a `git status` per keystroke.
                  Nothing selected → the SELF target: the pane's subject is THIS
                  folder, and it has no PREVIEW at all, so since D284 it lands on the
                  chat about the folder like any other folder subject. It falls back
                  to the neutral "Select a file to preview." hint only where neither
                  companion is offered (ListingPreviewPane's self branch, which
                  resolves no template and issues no stat) — and certainly not to a
                  "lone app": that concept was deleted with D264 and the comment here
                  outlived it by describing a resolution the self branch has never
                  performed. Its listing is not the answer either — that is already
                  on the left. */}
              <ListingPreviewPane
                key={paneKey(
                  paneSide,
                  fsPath,
                  paneRow ? paneRow.path : null,
                  sel.paths.length
                )}
                row={
                  /* `paneRow` is already "the settled row of a single-row
                     selection" (above), so it carries the count with it and needs
                     no second check here. */
                  paneRow ??
                  (sel.paths.length === 0
                    ? {
                        path: fsPath,
                        name:
                          fsPath.replace(/\/+$/, "").split("/").pop() || fsPath,
                        isDir: true,
                        self: true,
                      }
                    : null)
                }
                selCount={sel.paths.length}
                undecided={paneUndecided}
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
