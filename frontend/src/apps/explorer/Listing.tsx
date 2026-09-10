// Directory listing view with sortable columns and an in-folder search.
// Sort state lives in the URL (?sort=name|size|mtime&order=asc|desc) so a
// sorted listing is refresh-proof and bookmarkable like any other view state;
// the search query rides the URL the same way (?q=…). A non-empty query swaps
// the listing for flat, rank-ordered results served by the file index — see
// listing/useListingSearch for the fetch/memo/scan-on-demand pipeline.
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
//   bits.tsx               skeleton rows, ClipMark, GitMark, highlight, anchor
//   useDirListing.ts       /api/fs/list fetch, Load more, dir watch, new-row cue
//   useListingSearch.ts    index-backed search: fetch, memo, on-demand scan
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
} from "react";
import {
  IS_PANEL_PANE,
  IS_SNAPSHOT,
  embedUrlForFsPath,
  navigate,
  replaceSearch,
} from "@platform/lib/router";
import { dirname, normDir } from "@apps/explorer/lib/fs-actions";
import { useUrlVersion } from "@platform/lib/hooks";
import { getAppEntry } from "@platform/lib/api";
import { shortSha, snapshotListing } from "@platform/lib/snapshot-param";
import { useSnapshotForFolder } from "@apps/explorer/listing/useSnapshotForFolder";
import { acquireOverlay, releaseOverlay } from "@platform/lib/ui-overlay";
import { isMod } from "@platform/lib/platform";
import { basename, formatSize, formatMtime, formatMtimeFull } from "@platform/lib/format";
import { iconForEntry } from "@platform/ui/FileIcons";
import { getViewState, setViewState } from "@platform/lib/viewstate";
import { useFlip, FLIP_KEY_ATTR } from "@platform/lib/flip";
import { useClipboard } from "@apps/explorer/lib/fs-clipboard";
import ContextMenu from "@platform/ui/ContextMenu";
import type { OverflowEntry } from "@apps/explorer/BarMenu";
import { PromptDialog, ConfirmDialog } from "@apps/explorer/FsDialogs";
import ListingPreviewPane from "@apps/explorer/ListingPreviewPane";
import { AccessDenied, isAccessDenied } from "@apps/explorer/AccessDenied";
import { resultCountLabel } from "@apps/explorer/listing/result-cap";
import { claimFolderChrome } from "@apps/explorer/listing/folder-chrome";
import { useTypedPathAddress } from "@apps/explorer/listing/useTypedPathAddress";
import { useCompletion } from "@apps/explorer/listing/useCompletion";
import { showingSearchHits } from "@apps/explorer/listing/search-body-mode";
import { contractHome, useHome } from "@apps/explorer/listing/home-path";
import { formatElapsed } from "@apps/explorer/lib/home-search";
import { SearchField } from "@apps/explorer/SearchField";
import { searchSlot, subscribeSearchSlot, inSearchSlot } from "@apps/explorer/search-slot";
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
  GitMark,
  renderHighlightPath,
  measureScrollAnchor,
} from "@apps/explorer/listing/bits";
import { gitRowClass } from "@apps/explorer/listing/git-mark";
import { usePreviewPane } from "@apps/explorer/listing/pane";
import {
  activePaneSide,
  paneKey,
  paneReopenedByUrl,
  paneSideList,
  paneSideParam,
  parsePaneSide,
  type PaneSideChoice,
  type PaneSideState,
} from "@apps/explorer/listing/pane-side";
import { getSideHidden, setSideHidden } from "@apps/explorer/lib/side-hidden-store";
import { useDirMode } from "@apps/explorer/lib/dir-mode";
import { takeClaudeAsk, claudeEntryReady } from "@apps/explorer/lib/claude-ask";
// The flag module DIRECTLY, not the barrel: the barrel pulls `ChatMount` (and
// through it the chat's lazy boundary) into this file's graph for one boolean,
// which is the very thing feature-flag.ts's header says it is separate to avoid.
import { useNativeChatFlag } from "@apps/claude/feature-flag";
import {
  pendingClaudeAskVersion,
  subscribePendingClaudeAsk,
  takePendingClaudeAsk,
} from "@apps/explorer/lib/pending-claude-ask";
import { SideToggleButton } from "@apps/explorer/SideChrome";
import { EntryActionsMenu, canonEntryPath } from "@apps/explorer/EntryActionsMenu";
import { McpDialog } from "@apps/explorer/McpDialog";
import { withNoFocus } from "@platform/lib/frame-focus";
import { unavailableReason } from "@platform/lib/mode-visibility";
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
import { statusLine } from "@apps/explorer/listing/status-line";
import { useDirListing } from "@apps/explorer/listing/useDirListing";
import { useListingSearch } from "@apps/explorer/listing/useListingSearch";
import { useIndexStatus } from "@platform/lib/index-status";
import { searchCaveat } from "@apps/explorer/listing/index-caveat";
import { useListingSelection } from "@apps/explorer/listing/useListingSelection";
import { useFileOps } from "@apps/explorer/listing/useFileOps";
import { useListingShortcuts } from "@apps/explorer/listing/useListingShortcuts";
import { EmptyResultMessage } from "@apps/explorer/listing/empty-result";
import {
  ZERO_MATCH_OFFER_PATH,
  isZeroMatchOfferPath,
  settledZeroMatchOffer,
} from "@apps/explorer/listing/zero-match-offer";

// (The folder-entry rule is the SERVER's — `app_listing.app_entry`, D301: the
// first top-level page carrying `<meta name="fused-app">`. A filename tells
// the client nothing under the marker rule, so the "Open in project" button
// asks GET /api/apps/entry instead of re-deriving anything from row names.)

// The window global the injected runtime calls to hand this pane a prompt the
// git companion's "Fix with AI" button built for a failed operation
// (static/runtime.js `noteAskClaude`/`pullClaudeAsk`, reached from the git
// template as `window._fusedAskClaude` and from the claude template as
// `window._fusedTakeClaudeAsk`). Same ancestor-global shape Preview.tsx
// declares for the file sidebar's copy of this pair — this is the folder
// pane's.
declare global {
  interface Window {
    _fusedClaudeAsk?: (text: unknown) => void;
    _fusedClaudeAskTake?: () => string | null;
  }
}

export default function Listing({
  fsPath,
  provisional = false,
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
  // `barChrome`: this Listing IS the explorer's folder view — the one under
  // the crumb bar, whose layout zone it therefore claims (see
  // listing/folder-chrome.ts). The splits go away and the path `···` renders
  // in this listing's search row instead of at the far end of the bar. False
  // for every other host: the learn variant has no crumb bar to claim, and a
  // panel pane's Listing sits under a pane bar that carries its own splits and
  // its own `···`.
  barChrome?: boolean;
}) {
  // --- browsing this folder under a git snapshot (`_snapshot`) --------------
  // The resolution itself lives in useSnapshotForFolder (listing/), extracted
  // there so it can be driven through the listing's own hook harness rather
  // than only through this 2100-line component. `useUrlVersion()` is passed
  // in as a caller-supplied number, not read by the hook itself — see that
  // hook's own comment for why (code review finding B5: a commit selection
  // is a `replaceSearch`, which does not dispatch `fused:navigate`, only
  // `fused:urlchange`; keyed on `[fsPath]` alone, the resolution never
  // re-ran for a selection made while `fsPath` itself stayed put). This
  // closes the gap in both directions: Preview.tsx's own `backToLive`/
  // selection writes reach a mounted Listing even though neither writer is
  // Listing's own state, and Listing's own `backToLive` reaches a mounted
  // Preview the same way.
  const urlVersion = useUrlVersion();
  const { resolvedSnapshot, backToLive } = useSnapshotForFolder(fsPath, urlVersion);
  // Under an active snapshot whose app folder actually ENCLOSES this folder,
  // list the extracted tree instead of the live one — the same rewrite rule
  // static/runtime.js applies to every template read, so the two cannot
  // disagree about what a snapshotted read means. Elsewhere (this folder is
  // outside the app, or nothing has resolved) `listPath` is just `fsPath`.
  // The decision itself is `snapshotListing`, a pure function tested directly
  // (Listing.test.tsx) rather than only through this component.
  const { inSnapshot, listPath } = snapshotListing(fsPath);

  const { state, refresh, refetch, loadMore, loadingMore, newNames } =
    useDirListing(fsPath, listPath);

  // Sort lives in the URL; mirror it in state so clicks re-render without a
  // navigation (vanilla re-ran renderListing after its replaceState).
  const [{ sort, order }, setSortState] = useState<{
    sort: SortKey;
    order: SortOrder;
  }>(() => resolveSort(fsPath));
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fsPath]);
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

  // Decision 1: the crumbs shown inside the merged search field while it is
  // empty need home, the same way Breadcrumb.tsx's own strip does, to
  // contract a path under it to "~". Decision 5's typed-address resolution
  // (below) also resolves a leading "~" against it. `useHome` (home-path.ts)
  // is the one `/api/config` lookup shared with FileSearchField.tsx's own
  // box; unresolved (undefined) just means every crumb shows the full path
  // until it lands, and a "~"-led query is never treated as an address
  // until it does.
  const home = useHome();

  const {
    query,
    q,
    setQuery,
    searching,
    isPathQuery,
    isStale,
    behind,
    requestFailed,
    awaitingCommit,
    scanPending,
    requestComing,
    spinner,
    rescanPending,
    searchState,
    prefetchIndex,
    hits,
    searchBase,
    displayHits,
    visibleHits,
    rowsAnswerQuery,
    cappedAway,
    reason,
    mode,
    gateOpen,
    commitSearch,
    rerunQuery,
  } = useListingSearch(fsPath, home, refresh);

  // Decision 5: is the typed query itself a filesystem address? Resolved
  // independently of the ranked search above — Enter checks this first.
  const typedAddress = useTypedPathAddress(query, fsPath, home);

  // Decision 2: the completion dropdown. `completion.target` is null for a
  // query that isn't path-shaped at all (a plain filter word, a glob) —
  // that's when there is no dropdown, not merely an empty one. The dropdown
  // ITSELF — the highlight, the dropdown-open predicate, accepting a row —
  // is SearchField's own local UI state (a chip of interaction chrome, not
  // search-result state); this call stays here, alongside the other search
  // hooks, and its result is simply handed down as a prop.
  const completion = useCompletion(query, fsPath, home);

  // Scan state for the search box's "indexing…" caveat. Gated on `searching`
  // so an idle listing never polls.
  const indexScan = useIndexStatus(searching);

  // Search hits vs. the folder's own rows (listing/search-body-mode) — the
  // one place this choice is made, computed once here (not re-derived per
  // use) and read by the column count, the <thead>, the body branch and the
  // match-count chip further down, AND by `navRows`/`rowCtxByPath`/
  // `listingLoaded`/the auto-select effect below: a path-shaped query
  // (`isPathQuery`, above) is `searching` but never gets an answer
  // (useListingSearch.ts never asks), so every one of those has to agree it
  // is not showing hits either, or arrow-key nav would walk an empty list
  // over a folder plainly still on screen.
  const showsSearchHits = showingSearchHits(searchState, awaitingCommit);

  // A committed PATTERN (glob) search that has settled on genuinely zero
  // hits (`reason === ""` — anything else is an index-gap message,
  // EmptyResultMessage's own job, and broadening the pattern wouldn't fix an
  // uncovered folder) offers to rerun itself with the first rung of
  // glob-broaden.ts's ladder that actually widens the query. `null` here
  // means either there is nothing settled-and-empty to offer for, or
  // `broadenGlobOffer` walked every rung and found nothing left to widen —
  // both read identically to callers below: no offer.
  //
  // `settledZeroMatchOffer` (zero-match-offer.ts) owns the derivation
  // itself, not this call site: `mode`, `reason` and `displayHits` are read
  // off the search's own `answer`, which only changes when a new answer
  // lands, but the box can move the query past that answer well before a
  // request for the new text has even gone out (the fetch effect's own
  // trailing debounce) — a window where `scanPending` stays false and
  // `searchState` still reads "ok" for the PREVIOUS query. Passing
  // `rowsAnswerQuery` in is what closes that gap: it is the one flag that
  // already compares the rows on screen against `q` directly, so this
  // offer cannot be computed against a query nothing has checked yet.
  const broadenOffer = settledZeroMatchOffer({
    showsSearchHits,
    rowsAnswerQuery,
    searchState,
    scanPending,
    displayHitsLength: displayHits.length,
    mode,
    reason,
    q,
  });
  const rerunBroadenedSearch = () => {
    if (broadenOffer !== null) rerunQuery(broadenOffer.pattern);
  };

  // **THESE TWO FLAGS ARE NOW THE WHOLE of whether there is a pane** —
  // `pane.on` is exactly `paneEnabled` since D282 deleted the width gate, so
  // "is this a Listing that has a pane" is a question about the SURFACE and
  // never about pixels.
  //
  // There used to be a THIRD flag here, `embedded` — this Listing rendering
  // INSIDE another view's pane rather than as a top-level surface, the no-
  // nesting guard for the preview pane's own `_listing` mode. That mode
  // embedded a real, second `Listing` inside this one's pane so a selected
  // folder could be peeked at without navigating into it — and D460 deleted
  // the entire selection-driven pane that mode belonged to (FS-11): the pane
  // shows the open folder's own companions now, never a selected row's
  // anything, so there is no longer a second `Listing` anywhere for this one
  // to be nested inside. The prop had no caller left once that branch was
  // gone, and is deleted with it rather than kept as a guard against a
  // nesting that can no longer happen.
  //
  // A FROZEN-TREE listing is the first of the two remaining cases: the
  // browsable snapshot (the `browse` framing of the removed timeline mode,
  // PT-14) is a whole shell loaded at `/explorer/embed/<tree>?snapshot=1`, so
  // its Listing is the page's OWN top-level one, inside the framing view's
  // preview column, where it would otherwise grow a preview pane INSIDE a
  // preview pane. `?preview=false` used to stop it and was dropped with the
  // toggle it belonged to, on the reasoning that the width decides — true for
  // a listing that owns its window, false for one handed a column by a
  // framer. *That reasoning is doubly dead now: with the width gate deleted
  // (D282) the framed listing would grow a pane at ANY column width, so this
  // flag is not a refinement of a measurement but the whole answer.*
  //
  // `snapshot=1` and not a second param of its own: the framing flag has
  // exactly one producer, and that producer is a template framing this listing
  // in its own column. A flag that could only ever be written beside another
  // one is the "three places to agree about one bit" the pane's own history
  // (pane.ts) is a warning about.
  //
  // A PANEL PANE is the second, and it is the same shape of blind spot as the
  // snapshot: a pane is a whole shell at `/explorer/embed/<path>`, so its
  // Listing is that frame's own top-level one — `barChrome` true, everything
  // about it says "I own this window". What it does not own is the layout:
  // the user split it, so a split-right of a folder grew two half-width
  // listings each with their own preview. Four columns where the user asked
  // for two — and no width test could ever have objected, because the width
  // was genuinely there. IS_PANEL_PANE is the host-side question a measurement
  // cannot answer (see router.ts, including why `IS_EMBED` — which is also
  // every TAB, where the pane is right and stays — is the wrong flag here).
  //
  // Switching it off HERE is the whole feature: `pane.on` IS this predicate
  // (D282 left nothing else in it), so one flag takes the slot, the divider, the
  // closing chevron on the pane's header, the reopening SideToggleButton in the
  // search row and the two `useDirMode` companion probes with it. Nothing about the
  // ROWS changes: a pane's listing still selects, arrow-keys, and opens on a
  // single click/Enter — opening a file in a pane replaces that pane's
  // document, which is the point.
  const paneEnabled = !IS_SNAPSHOT && !IS_PANEL_PANE;
  // Drag-to-close hands off to the same `_side=off` the pane header's close
  // button writes (`closeSide`, below) — one vocabulary for "the pane is shut",
  // whichever gesture said it. A closure, called only from pointer events, so
  // its later declaration is already initialised by the first possible call.
  const { pane, splitRef, onDividerPointerDown } = usePreviewPane(
    paneEnabled,
    () => closeSide()
  );

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
    parsePaneSide(
      paneEnabled ? new URLSearchParams(location.search).get("_side") : null,
      getSideHidden()
    )
  );
  // The folder half of the same D495 reconciliation Preview.tsx does for the
  // file view — the pure rule is `paneReopenedByUrl` (listing/pane-side.ts).
  // `paneEnabled` guards it the same way `setSide` below is guarded: a
  // snapshot or panel pane never reads the real `_side` param in the first
  // place (see the `parsePaneSide` call above), so it can never hit the
  // explicit branch and has nothing to reconcile.
  useEffect(() => {
    if (paneEnabled && paneReopenedByUrl(getSideHidden(), sideState)) {
      setSideHidden(false);
    }
    // Mount only: reconciles the flag against what the URL asked for when
    // this folder opened, not on every render — `setSide` keeps it current
    // from here on.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
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
  // `mcp` for the same reason as `git`: the manifest it curates covers the FOLDER
  // (templates/mcp/condition.py). Not a pane mode — it gates the kebab's "MCP
  // config" row (below) and the dialog behind it.
  const folderMcp = useDirMode(paneEnabled ? fsPath : null, "mcp");
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
  //
  // MCP IS NOT A PANE MODE (listing/pane-side's PANE_SIDE_COMPANIONS): the
  // pane's switcher is a two-tab strip over Claude and Git (SideChrome's
  // SideTabs), and the MCP companion opens as a dialog off the search row's
  // kebab (EntryActionsMenu → McpDialog, the same arrangement the file preview
  // has). The `folderMcp` probe above feeds that row alone, through `mcpSrc`.
  const sideEntries = {
    claude: folderClaude.pending ? null : folderClaude.entry,
    git: folderGit.pending ? null : folderGit.entry,
    claudePending: folderClaude.pending,
    gitPending: folderGit.pending,
    claudeBound: folderClaude.bound,
    gitBound: folderGit.bound,
  };
  // The MCP dialog's document — the URL ListingPreviewPane built for the mcp
  // pane (`_file` is the folder, `_noopen=1` so the render is not recorded as an
  // app open), or null while the probe is out or where the folder is not an app.
  const mcpSrc =
    !folderMcp.pending && folderMcp.entry && folderMcp.entry.path !== null
      ? withNoFocus(
          `/render?path=${encodeURIComponent(folderMcp.entry.path)}` +
            `&_file=${encodeURIComponent(fsPath)}&_noopen=1`
        )
      : null;
  const [mcpOpen, setMcpOpen] = useState(false);
  useEffect(() => {
    setMcpOpen(false);
  }, [fsPath]);
  const paneOpen = pane.on && sideState.open;
  // One writer for both halves of the state, and it writes the URL only where the
  // listing owns one: a frozen-tree snapshot and a panel pane are each a whole
  // shell handed a column by something else (`paneEnabled` above), and neither
  // owns the address bar it happens to be inside of.
  //
  // Same gate on the session's hidden flag (`lib/side-hidden-store.ts`): a
  // snapshot or panel pane is not the addressable folder view either, so a close
  // inside one must not shut every OTHER open folder/file's sidebar for the rest
  // of the session.
  const setSide = (next: PaneSideState) => {
    setSideState(next);
    if (!paneEnabled) return;
    setSideHidden(!next.open);
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

  // Search input, so a keystroke anywhere in the listing can focus it — and
  // so useListingSelection/useListingShortcuts below can focus it directly
  // for type-to-search and their own shortcuts. Owned here rather than
  // inside SearchField (which the box's own JSX now lives in) because those
  // two hooks need the same node SearchField's own subscribeSearchFocusRequest
  // effect focuses — one ref, so every path to "the search input" agrees on
  // which element that is.
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

  // Claim the crumb bar for as long as this folder view is mounted: the splits
  // come off it, the path `···` renders in the search row below, and the bar
  // itself portals into `crumbSlotRef` — the top of THIS column — so the
  // preview pane beside it runs the full height of the window (see
  // listing/folder-chrome.ts).
  //
  // A layout effect: the claim moves the bar, and a passive effect would paint
  // one frame with it still spanning the window before it dropped into place.
  // Refs are attached before layout effects run, so the slot is there.
  const ownsBarChrome = barChrome;
  const crumbSlotRef = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    if (!ownsBarChrome) return;
    return claimFolderChrome(crumbSlotRef.current);
  }, [ownsBarChrome]);

  // …and the search row goes UP into that same bar, into the middle column
  // the path bar holds — one header strip in this column, matching the
  // pane's one across the divider (search-slot.ts). Non-null only once the
  // bar has rendered its target, which is only ever over a folder that
  // claimed the chrome; a host with no crumb bar (the app builder) keeps the
  // row in place as its own first strip.
  const barSearchSlot = useSyncExternalStore(subscribeSearchSlot, searchSlot, () => null);

  // No "Up" BUTTON beside the search box any more: the crumb strip above is
  // the same hop with a target the user can name, and the keyboard keeps its
  // own (Mod+Up / bare Backspace — see listing/useListingShortcuts).

  // Flat, ordered list of the paths the arrow keys step through: the rendered
  // search hits while `showsSearchHits`, otherwise the sorted listing. Keyed
  // off the same memoized arrays the table renders, so selection never drifts
  // from view.
  //
  // `showsSearchHits`, not raw `searching`: a path-shaped query is
  // `searching` but never gets an answer (useListingSearch.ts), so keying
  // this on `searching` alone would walk an empty `visibleHits` forever
  // instead of the folder rows actually on screen.
  const navRows = useMemo(
    () =>
      showsSearchHits
        ? visibleHits.map(({ entry }) => searchBase + "/" + entry.rel)
        : sortedEntries.map((entry) => base + "/" + entry.name),
    [showsSearchHits, visibleHits, sortedEntries, base, searchBase],
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
  // Keyed on `showsSearchHits` for the same reason `navRows` is above: a
  // path-shaped query is folder rows on screen, not a search mid-load.
  const listingLoaded = showsSearchHits ? true : state.status !== "loading";

  const {
    sel,
    selectedPath,
    selectedSet,
    selectOnly,
    clearSelection,
    selectPaths,
    toggleSelected,
    extendTo,
    pendingSelectRef,
  } = useListingSelection({
    fsPath,
    navRows,
    listingLoaded,
    rowsAnswerQuery,
    searchInputRef,
    rowCtxByPathRef,
    overlayOpenRef,
    // `globalKeys` defaults to true (useListingSelection.ts): there is no
    // caller left that ever passed false — the one that used to (`embedded`,
    // the preview pane's own nested `_listing` mode) is gone with D460.
    zeroMatchOffer:
      broadenOffer !== null
        ? { path: ZERO_MATCH_OFFER_PATH, onActivate: rerunBroadenedSearch }
        : null,
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
  // right-click does. Keyed off the arrays the table renders — `showsSearchHits`,
  // the same predicate `navRows` above branches on, and for the same reason.
  const rowCtxByPath = useMemo(() => {
    const m = new Map<string, RowCtx>();
    if (showsSearchHits) {
      for (const { entry } of visibleHits) {
        const path = searchBase + "/" + entry.rel;
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
  }, [showsSearchHits, visibleHits, sortedEntries, base, searchBase]);
  rowCtxByPathRef.current = rowCtxByPath;

  // OPENING A FOLDER SELECTS NOTHING (FS-16, D278). There is no folder
  // auto-select here and there is deliberately no code for one: a freshly opened
  // folder has an empty selection, and its pane shows the chat about the folder
  // regardless (FS-11, D460) — the pane no longer reads the selection at all, so
  // there is no "until the user picks a row" any more: picking a row changes
  // nothing about what the pane shows.
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
  // WHEN to ask. Not provisional — a scaffold's selection would be torn down
  // and re-made by the swap to the resolved listing a beat later. It does NOT
  // wait for `pane.on`: a selected top hit is worth having for Enter and the
  // arrow keys whether or not the window is wide enough to preview it.
  //
  // Whose selection it is — and in particular that a user's choice OUTLIVES a
  // query change — is `nextSearchSelection`'s to track, not this effect's. It
  // lived here as a ref that got cleared per query, which quietly threw the
  // user's selection away on the next keystroke; it is state with rules, so it
  // belongs somewhere it can be tested.
  const searchSelectRef = useRef(INITIAL_SEARCH_SELECT);
  useEffect(() => {
    // `showsSearchHits`, not raw `searching`: a path-shaped query never gets
    // a ranked answer, so there is nothing here for auto-select to rank —
    // `navRows` is the folder's own rows in that state (see its own comment).
    if (provisional || !showsSearchHits) return;
    const { state, select, clear } = nextSearchSelection(
      searchSelectRef.current,
      navRows,
      rowCtxByPath,
      sel,
      rowsAnswerQuery,
    );
    searchSelectRef.current = state;
    if (select !== null) selectOnly(select);
    // Withdrawing a selection this effect placed, because the rows it sits on
    // have stopped answering the query in the box (listing/selection). Left
    // standing it arms Enter and Cmd+Backspace on a file nobody is looking for.
    else if (clear) clearSelection();
  }, [provisional, showsSearchHits, navRows, rowCtxByPath, sel, selectOnly,
      clearSelection, rowsAnswerQuery]);

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

  // WHICH of the pane's three modes it is on. Resolved here, below the selection
  // even though it no longer READS the selection (D460) — it stays here because
  // it is folder-scoped state that lives alongside the other folder-scoped state
  // this component already holds (sideEntries, folderClaude/folderGit/folderMcp
  // above).
  //
  // **THE PANE'S SUBJECT IS THE OPEN FOLDER, ALWAYS** (D460, superseding
  // D280/D281/D284/D285's whole arc of "which selection state is the subject").
  // It used to be a question with an answer that moved: a selected directory row,
  // else nothing selected (the folder itself, D284), else a selected file (no
  // subject at all — `null`, which is why the code below used to carry a
  // `paneSubjectDir === null` branch and `openAppEntry` an `!paneSubjectDir`
  // guard). All of that read the selection to decide what the pane would show,
  // which is exactly the coupling D460 deletes: the subject is `base`, the open
  // folder, full stop — a plain string, never null, and it never changes
  // without a navigation. The indirection through a `paneSubjectDir` constant
  // is gone with the question it used to answer; `base` is used directly below.

  // The subject's entry page comes from the server (GET /api/apps/entry — the
  // one copy of the rule, `app_listing.app_entry`, D301), one request per
  // FOLDER OPEN — `base` only changes on a navigation, so this effect fires
  // once per folder rather than once per selection change the way it used to.
  //
  // Cleared to null before every fetch: the button must never point at the
  // previous folder's entry while the new one resolves, which is the
  // "whatever it points at is what the pane says it is about" rule.
  // Hidden-then-shown is the acceptable shape of that; pointing at the wrong
  // folder is not.
  const [appEntryPath, setAppEntryPath] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    setAppEntryPath(null);
    getAppEntry(base).then(
      (res) => alive && setAppEntryPath(res.entry),
      // An unreadable folder is "no entry page", never an error of its own: the
      // button simply does not appear.
      () => alive && setAppEntryPath(null),
    );
    return () => {
      alive = false;
    };
  }, [base]);

  // "Open in project" itself — desk add, then `/apps/<folder>` — is
  // EntryActionsMenu's row now, on the search row's kebab, gated on this answer.

  const paneSides = paneSideList(sideEntries);
  // UNDECIDED — this folder's companion probes have not answered yet (pane-side's
  // paneSideList returns an empty list, and only for that). The pane holds a
  // skeleton: resolving a side here would put the pill on `preview` while a chat
  // rendered under it regardless, and would then remount — and respawn
  // `agent.py` — the moment the probe landed.
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

  // --- the CLAUDE companion's seeded prompt (`window._fusedAskClaude`) -------
  // The `git` companion's "Fix with AI" button has no chat of its own — it
  // hands the prompt it built to whichever ancestor owns a Claude sidebar,
  // through the runtime's ancestor-window hop (static/runtime.js
  // `noteAskClaude`). This pane is one such ancestor for a FOLDER's own `git`
  // companion (Preview.tsx installs the file sidebar's copy); guarded on
  // `paneEnabled` for the same reason its declaration there is guarded on
  // `splitCapable` — a snapshot or panel pane with no pane at all has nothing
  // to open this into.
  //
  // THIS IS A PULL, NOT A PARAM ON THE COMPANION IFRAME'S SRC (review #804
  // round 2) — see Preview.tsx's copy of this comment for the full argument.
  // In short: a param baked into `ListingPreviewPane`'s src, kept "one-shot" by
  // a cache keyed on `paneKey(paneSide, fsPath)`, still replayed on any
  // remount that key comparison could not tell apart from a genuinely new ask
  // — closing and reopening the pane on the SAME folder is a fresh mount with
  // an unchanged key, and so is toggling `git` -> `claude` -> `git` -> `claude`
  // without a second click. So the prompt lives here as plain state instead,
  // and the CLAUDE TEMPLATE pulls it at its own boot
  // (`window._fusedClaudeAskTake`, via the claude template's
  // `_fusedTakeClaudeAsk` / static/runtime.js `pullClaudeAsk`) — consumption is
  // then a property of WHEN a pull happens, not something a cache reconstructs
  // from a key.
  //
  // A REF, not state: it must survive from the moment it arrives to whichever
  // later boot pulls it, and must never itself cause a render —
  // `selectSide` already does that.
  const claudeSeedRef = useRef<string | null>(null);
  // A new ask can arrive while the pane is ALREADY showing claude on the SAME
  // folder — a second "Fix with AI" click without switching companion or
  // folder first — and `paneKey(paneSide, fsPath)` alone cannot tell that
  // apart from an unrelated re-render: neither `paneSide` nor `fsPath` changed,
  // so `ListingPreviewPane`'s key would not either, and nothing would remount
  // it to make its boot pull the new text. Bumped on every incoming ask and
  // folded into the key passed down (below), the same fix Preview.tsx's
  // `claudeAskInstance` is for the sidebar's copy of this gap.
  const [claudeAskInstance, setClaudeAskInstance] = useState(0);
  // WHO PULLS THE ASK (Preview.tsx carries the same pair for the file sidebar).
  // Flag OFF, the claude template pulls it out of `window._fusedClaudeAskTake`
  // at its own boot and nothing here may touch it. Flag ON there is no boot to
  // pull from, so the host reads-and-clears once per ask — a LEDGER and not a
  // memo, because the pull IS the clear (lib/claude-ask.ts) — and hands the text
  // down as a prop.
  // In a COMMITTED EFFECT, not the render body: the pull is destructive, so a
  // render React discards would eat the ask. And the pane is keyed on the
  // DELIVERY rather than on the arrival, because the effect lands after the
  // render that saw the bumped instance — keying on the arrival remounted the
  // pane before there was anything to boot it with (Preview.tsx carries the
  // same pair, with the full argument).
  // TRI-STATE, and the key below is why: `null` is "the prefs read has not
  // landed", not "legacy". Flattened to a boolean for the ask ledger, where
  // "not asked yet" is honestly "no" (feature-flag.ts).
  const nativeChatState = useNativeChatFlag();
  const nativeChat = nativeChatState === true;
  const [askDelivery, setAskDelivery] = useState<{ text: string; seq: number } | null>(null);
  const pulledFor = useRef(-1);
  useEffect(() => {
    if (!nativeChat || pulledFor.current === claudeAskInstance) return;
    pulledFor.current = claudeAskInstance;
    const text = takeClaudeAsk(claudeSeedRef);
    if (text) setAskDelivery({ text, seq: claudeAskInstance });
  }, [nativeChat, claudeAskInstance]);
  // Handed over exactly once: `closeSide` then reselecting claude remounts
  // `ListingPreviewPane` at the same key, and a ledger still holding the text
  // would replay a stale prompt into the new conversation.
  const deliveredAsk = useRef(-1);
  useEffect(() => {
    if (askDelivery) deliveredAsk.current = askDelivery.seq;
  }, [askDelivery]);
  const nativeAsk =
    askDelivery && deliveredAsk.current !== askDelivery.seq ? askDelivery.text : null;
  // Whether claude is confirmed showable for THIS folder right now — the
  // exact question `selectSide("claude")` would answer by hand, with
  // `claudeEntryReady` additionally requiring the gate to have SETTLED, not
  // merely exist (review #804 round 3 finding 3: a still-PENDING verdict is
  // not a "no", but promising delivery for it would store a seed nothing is
  // about to pull — see lib/claude-ask.ts's own header for the full argument,
  // shared with Preview.tsx's copy of this problem).
  const claudeReady = claudeEntryReady(sideEntries.claude, !!sideEntries.claudePending);
  // The action this render would take if an ask arrives, kept in a ref
  // updated on EVERY render (no dependency array) — review #804 round 3
  // finding 6. The export installed below is a stable wrapper that only ever
  // reads `claudeAskActionRef.current` at CALL time, so it never needs
  // reinstalling to stay current; the ORIGINAL version of this hook
  // (reinstalled only when `paneEnabled` changed) called a `selectSide`
  // closed over whatever `paneSides` was at THAT install — a list that
  // resolves asynchronously from the companion gates and can legitimately
  // change without `paneEnabled` doing so, which risked exactly the same
  // "wrong `_side` spelling written on a later reload/bookmark" bug
  // Preview.tsx's copy of this fix documents in full.
  const claudeAskActionRef = useRef<(text: string) => boolean>(() => false);
  useEffect(() => {
    claudeAskActionRef.current = (text: string) => {
      if (!claudeReady) return false;
      claudeSeedRef.current = text;
      setClaudeAskInstance((n) => n + 1);
      // Switches the pane to Claude — REPLACING whatever companion (most often
      // `git`, the one that just failed) was showing. The error and repo state
      // the git pane knew are already folded into `text`, so nothing is lost
      // by the git pane going away.
      selectSide("claude");
      return true;
    };
  });
  useEffect(() => {
    if (!paneEnabled) return;
    window._fusedClaudeAsk = (text: unknown) => {
      if (typeof text !== "string" || !text) return false;
      return claudeAskActionRef.current(text);
    };
    // The other half of the pull: the claude template's own boot calls this to
    // collect whatever is pending. `takeClaudeAsk` (lib/claude-ask.ts, shared
    // with Preview.tsx's copy of this hook) is what actually reads-and-clears.
    window._fusedClaudeAskTake = () => takeClaudeAsk(claudeSeedRef);
    return () => {
      delete window._fusedClaudeAsk;
      delete window._fusedClaudeAskTake;
    };
    // The wrapper itself never goes stale just by staying installed — see
    // `claudeAskActionRef`'s own comment just above.
  }, [paneEnabled]);
  // A still-pending ask abandoned by a folder navigation that lands BETWEEN
  // storing the seed (once `claudeReady` confirmed it was about to be
  // delivered) and the switch actually completing must not survive into an
  // unrelated later boot on a DIFFERENT folder.
  useEffect(() => {
    claudeSeedRef.current = null;
  }, [fsPath]);

  // The other side of a "Fix with Claude" staged from OUTSIDE this pane
  // entirely — a repo-updates row in the activity card (shell/
  // RepoUpdatesDock.tsx), which stages `{path, prompt}`
  // (lib/pending-claude-ask.ts) and navigates here rather than calling
  // `window._fusedClaudeAsk` the way the git companion's OWN button does,
  // for the same reason Preview.tsx's copy of this hook gives: that export
  // only exists once a surface for this exact path is already mounted.
  // Gated on `claudeReady`, not merely mounted — Preview.tsx installs its own
  // copy of this same pull for the file sidebar; both must fire independently
  // (the "Lockstep" this pair is, per its own module comment) or a folder
  // opened one way silently drops the prompt the other way would have shown.
  //
  // `askVersion` (finding 17b, code review 2026-08-27) covers the case
  // `[fsPath, claudeReady]` alone misses: a SECOND stage for the SAME path
  // while this pane never left it (the common case — the user is usually
  // already looking at the repo whose card just failed) changes neither dep,
  // so without it this effect would never re-run and the prompt would sit
  // unseen until it expires. `pending-claude-ask.ts`'s own header has the
  // full reasoning.
  const askVersion = useSyncExternalStore(
    subscribePendingClaudeAsk,
    pendingClaudeAskVersion,
    pendingClaudeAskVersion,
  );
  useEffect(() => {
    if (!claudeReady) return;
    const prompt = takePendingClaudeAsk(fsPath);
    if (prompt) claudeAskActionRef.current(prompt);
  }, [fsPath, claudeReady, askVersion]);

  // A HABITUAL DOUBLE-CLICK NOW DOUBLE-OPENS, and this is the guard against it.
  // Single-click-open (D460) means the first press of what a lifetime of
  // double-clicking trained someone to do already navigates on its release —
  // and when that navigation is INTO A FOLDER, this same `Listing` instance
  // re-renders with the new `fsPath` rather than unmounting (shell/App.tsx
  // renders it unkeyed), so the second press of the habitual pair lands on
  // whatever row the NEW folder painted under a cursor that has not moved —
  // and opens THAT, which nobody asked for and nothing about it looks like a
  // mistake to whoever it happens to.
  //
  // Set only from the RELEASE that actually opened something (onRowPointerUp
  // below), and checked at the top of both press paths — this component's own
  // onRowPointerDown, and useMarquee's capture-phase arbiter, which runs
  // BEFORE it and has to honour the same window itself (drag-drop's
  // `pressIsSuppressed`) or a press this ref is about to make inert still
  // reaches it and starts a real move-drag. Neither reads select, toggle, or
  // extend for it, because the whole point is that this press should not be
  // read as an action on this (freshly rendered, unrelated) row at all. A
  // plain timestamp compared against `Date.now()` rather than a timer:
  // nothing has to be scheduled or cleared, and a press that never comes
  // finds the ref simply stale.
  //
  // The window is the same rough length a native double-click's is — long
  // enough to catch the habitual second click, short enough that a genuinely
  // deliberate fast click a folder-hop later is the rare cost, not the norm.
  //
  // THIS IS NOT A DOUBLE-CLICK TIMER RESTORED FOR ITS OWN SAKE — there is
  // still no delay before a plain press's own release opens IT (D460's whole
  // point stands: nothing here waits to see if a second click arrives before
  // acting on the first). It exists purely to absorb the SECOND press of a
  // pair that a habit built for the old model still sends, aimed at a row
  // that just changed out from under it.
  const OPEN_SUPPRESS_MS = 400;
  const suppressPressUntilRef = useRef(0);

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
    suppressPressUntilRef,
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
    // `globalKeys` defaults to true (useListingShortcuts.ts) for the same
    // reason as the selection hook's call above.
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
    if (Date.now() < suppressPressUntilRef.current) return;
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

  // SINGLE CLICK OPENS — in every listing, at every window width (D460). A
  // plain press SELECTS (onRowPointerDown above); the matching RELEASE OPENS,
  // provided the press never left the row (no drag, no sweep). Only a PLAIN
  // press opens: Shift and Mod still mean range and toggle, never navigation,
  // so a modified click keeps building a selection instead of taking the user
  // somewhere.
  //
  // This used to be gated to an EMBEDDED listing only (the preview pane's own
  // `_listing` mode) while every other listing waited for a double-click. The
  // gate existed because a plain click had a second job to protect: the pane
  // used to follow the SELECTION, so a click that navigated away would have
  // made a row's own preview unreachable, and a folder was worst hit of all
  // (FS-11's old text). D460 removed that coupling — the pane now shows the
  // open folder's own companions and never the selection — so there is nothing
  // left to protect, and the single-click model that already worked in the
  // pane extends to every listing.
  const openOnRelease = (action: RowPressAction) =>
    action === "select" || action === "defer";

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
  // The release is also where every listing OPENS on a single click
  // (openOnRelease above). Both halves want the same two facts — same row, press
  // stayed still — so they share the one handler and the one slop test.
  const onRowPointerUp = (e: React.PointerEvent, path: string) => {
    // The zero-match glob-broadening offer row (Listing.tsx's settled-empty
    // body branch below) is not a real row: it has no `pressRef`/press-slop
    // tracking, no `rowCtxByPath` entry, and must never call `selectOnly`
    // (there is nothing to select — see useListingSelection.ts's own
    // `zeroMatchOffer` prop for the Enter half of this same branch). It only
    // wires `onPointerUp`, so this check has to come before the `pressRef`
    // read below, which the offer row never populated in the first place.
    if (isZeroMatchOfferPath(path)) {
      // Same guard every real row's press already gets (onRowPointerDown,
      // above): a right-click or middle-click landing on this row must not
      // rerun the broadened search, and a right-click also has its own job
      // — opening the background context menu — that this would otherwise
      // step on.
      if (e.button !== 0) return;
      rerunBroadenedSearch();
      return;
    }
    const press = pressRef.current;
    pressRef.current = null;
    if (!press || press.path !== path) return;
    if (passedDragSlop({ x: press.x, y: press.y }, { x: e.clientX, y: e.clientY })) return;
    if (press.action === "defer") selectOnly(path);
    if (openOnRelease(press.action)) {
      const row = rowCtxByPath.get(path);
      if (row) {
        // Arm the double-click guard above BEFORE navigating: the point is to
        // catch the habitual second press, which can arrive before this
        // function returns on a fast enough click.
        suppressPressUntilRef.current = Date.now() + OPEN_SUPPRESS_MS;
        navigate(row.path, { isDir: row.isDir });
      }
    }
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

  // The folder's `⋮` used to sit on the MODIFIED column header (and on the
  // search view's Path header, and on an empty folder's bare strip), opening
  // `barMenu()`. It is gone: the bar's kebab (EntryActionsMenu, in the search
  // row) carries that same list under the app rows, so a folder has ONE `⋮`.
  // `barMenu()` itself stays the single source — the crumb bar's right-click
  // opens it too (publishTopbarMenu).

  // --- table body -----------------------------------------------------------

  let body: React.ReactNode;
  // Column headers describe columns of data; over an empty folder they label
  // nothing and just push the "Empty directory" message down (most visible in
  // the preview pane, where NAME/SIZE/MODIFIED sat above one line of text).
  // Set by the empty branch below, read by the <thead> render.
  let emptyDir = false;
  // `showsSearchHits` (computed above, alongside the search hooks) is read
  // here by the column count, the <thead>, the body branch, and the
  // match-count chip together so the four can never disagree.
  // Every row that spans the table, in the mode it is being rendered for
  // (listing/types columnCount): three columns normally, one while showing
  // search hits.
  const cols = columnCount(showsSearchHits);
  if (showsSearchHits) {
    if (searchState.status === "error") {
      body = (
        <tr>
          <td colSpan={cols} className="status-message error">
            Search failed: {searchState.message}
          </td>
        </tr>
      );
    } else if (displayHits.length) {
      // Rows exist: the settled ranking for this query, or (never-blank) the
      // previous query's rows standing in while the next answer is in flight.
      body = (
        <>
          {visibleHits.map(({ entry, positions }) => {
            const childPath = searchBase + "/" + entry.rel;
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
                  {/* The span hugs the icon+name so a long name ellipsizes
                      inside it, AND it is the row's drag SOURCE: the item is
                      its own name cell, so a press here starts a move-drag
                      whether or not the row was already selected, and a press
                      anywhere else in the row is marquee ground instead
                      (useMarquee's pressedRow, drag-drop's pressStartsDrag). */}
                  <span className="row-handle" data-fs-drag-handle="1">
                    <span className="icon">
                      {iconForEntry(
                        entry.rel.split("/").pop() ?? entry.rel,
                        entry.is_dir,
                      )}
                    </span>
                    <span className="search-path">
                      {renderHighlightPath(entry.rel, positions)}
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
    } else if (scanPending || searchState.status === "pending") {
      // Nothing to show yet: either the folder is being scanned on demand
      // (listing/index-source) or the request is simply in flight. Without
      // this, an empty answer would render as a confident "No matches" for a
      // moment on every keystroke.
      body = (
        <tr>
          <td colSpan={cols} className="status-message">
            Searching…
          </td>
        </tr>
      );
    } else {
      // A settled, empty answer. `reason` is only ever non-"" when the index
      // could not cover this folder at all (mount / package / ignored /
      // disabled / no Full Disk Access / still building / gave up waiting) —
      // "No matches" on its own would blame the user's files for the app's
      // state in every one of those cases. `EmptyResultMessage` (below) is
      // the render for each of those, sharing `indexGap`'s classification and
      // copy with the home page's own search box rather than inventing new
      // wording for the same states.
      body =
        broadenOffer !== null ? (
          // The glob-broadening offer: reads as an offer, not a matched
          // file, by reusing this same `status-message` row shape (already
          // the treatment for every OTHER non-file row above — "Searching…",
          // the capped-away count) rather than a `fh-row`. Deliberately
          // brief: it does not echo the original query back (that's what
          // "given search term" stands in for) and does not name which
          // dimension the offered pattern relaxes — both would make this
          // row long enough to be the very thing finding 5's wrap fix has
          // to catch at a narrow pane. glob-broaden.ts's rung label still
          // exists and is still ordered and named there — it just isn't
          // rendered here. Wired through `onRowPointerUp` (the same call
          // site every real row activates from) with the reserved sentinel
          // path rather than a parallel click handler — see
          // zero-match-offer.ts.
          <tr className="zero-match-offer-row">
            <td colSpan={cols} className="status-message">
              <span className="zero-match-offer">
                <span className="zero-match-offer-text">
                  No matches for given search term. Widen instead to:{" "}
              <button
                type="button"
                className="fh-link-button"
                onPointerUp={(e) => onRowPointerUp(e, ZERO_MATCH_OFFER_PATH)}
                onPointerDown={(e) => {
                  // Only `onPointerUp` is wired (see onRowPointerUp's own
                  // sentinel branch, above) — but a bare click still fires
                  // pointerdown first, and leaving it unhandled would let
                  // the press fall through to whatever native default a
                  // `<button>` inside this table carries. Nothing else here
                  // reads pointerdown for this row (no pressRef entry is
                  // ever created for the sentinel path), so this only needs
                  // to stop that default, not track anything.
                  e.preventDefault();
                }}
                onClick={(e) => {
                  // A focused button never satisfies useListingSelection's
                  // `navActive` (search input or document body/root only),
                  // so a keyboard Tab+Enter/Space never reaches the
                  // document-level Enter handler's own offer branch — this
                  // is the one path that lets keyboard activation work at
                  // all. A pointer interaction is already handled by
                  // `onPointerUp` above; this must not run a second time for
                  // one. `detail` is the click count a pointer device
                  // reports (always >= 1) — a keyboard-synthesized click
                  // reports 0, which is the one case `onPointerUp` never
                  // sees at all (no pointer events fire for it).
                  if (e.detail !== 0) return;
                  rerunBroadenedSearch();
                }}
              >
                <strong>{broadenOffer.pattern}</strong>
              </button>
                </span>
                {/* The dropdown's own `↵` vocabulary
                    (`.listing-completion-hint`), on the one row in the body
                    that Enter acts on. Without it the offer states a pattern
                    and leaves the reader to guess there is a way to run it —
                    and the row carries no other affordance, since the
                    highlight it wears is a colour, not a control. */}
                <span className="listing-completion-hint" aria-hidden="true">
                  ↵
                </span>
              </span>
            </td>
          </tr>
        ) : (
          <tr>
            <td colSpan={cols} className="status-message">
              <EmptyResultMessage
                reason={reason}
                scanning={indexScan === null ? null : indexScan.scanning}
                filesScanned={indexScan?.files ?? 0}
              />
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
    //
    // A REFUSED read (403 — macOS TCC, mode bits) is not a failure of ours to
    // report in red: it gets the plain access card with the Full Disk Access
    // strip, so the fix sits where the error is (AccessDenied.tsx).
    body = provisional ? (
      skeletonRows(8)
    ) : isAccessDenied({ status: state.httpStatus, message: state.message }) ? (
      <tr>
        <td colSpan={cols} className="status-message">
          <AccessDenied path={fsPath} />
        </td>
      </tr>
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
            gitRowClass(entry.git) + // git tints the NAME, not the row
            (newNames.has(entry.name) ? " row-new" : "") + // brief dir-watch tint
            (selectedSet.has(childPath) ? " selected" : "") +
            (childPath === selectedPath ? " lead" : "") + // scroll-into-view marker
            (cutSet.has(childPath) ? " cut" : "") +
            (copiedSet.has(childPath) ? " copied" : "")
          }
          onPointerDown={(e) => onRowPointerDown(e, childPath)}
          onPointerUp={(e) => onRowPointerUp(e, childPath)}
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
            {/* The item's drag source — see the search-hit row above. */}
            <span className="row-handle" data-fs-drag-handle="1">
              <span className="icon">
                {iconForEntry(entry.name, entry.is_dir)}
              </span>
              {/* Its own span so a row hover can underline the name alone
                  (explorer.css, tr.row:hover .name-text) — the one thing in
                  an otherwise-plain row a hover singles out as "go here". */}
              <span className="name-text">{entry.name}</span>
            </span>
            <GitMark status={entry.git} />
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

  // SPEC-omnibox-search-affordance.md scope item 5: the banner that used to
  // sit here — "press Enter to open that folder and search" for an
  // uncommitted path-shaped/escaping query, or the "No such file or
  // folder" refusal for one already committed and missing — is gone. Both
  // moved into SearchField.tsx's own completion dropdown as rows
  // (search-action-rows.ts's `searchAffordance`), which is exactly where
  // the rest of this spec's guidance is moving TO, not a special case of
  // its own any more. Removing it also gets the file list its full height
  // back — the banner pushed every row down by one.

  // --- search match count (inline in the search row) ------------------------
  //
  // Two strings per state: a TERSE one to show and the full sentence to say.
  // The chip is pinned inside the input's right edge, so every character it
  // spends is a character the query cannot use — and since the row moved up
  // into the crumb bar (search-slot.ts) it is competing with the path as well.
  // "1,204 matches · 45,110 scanned…" was most of a narrow box, so the chip
  // stays terse everywhere it can: compact notation for the count, and the
  // "top N of M" branch names its own numbers well enough without a noun.
  // The plain branch, though, is two bare numbers once a latency or caveat
  // suffix joins it (e.g. "10 · 45 ms"), which reads as unrelated figures —
  // so it alone carries "matches", pluralised off the raw hit count. The full
  // sentence (`resultCountLabel`) stays in the title and the aria-label.
  const compact = (n: number) =>
    n.toLocaleString(undefined, { notation: "compact", maximumFractionDigits: 1 });

  let searchCount: string | null = null;
  let searchCountFull: string | undefined;
  // The count and the caveat/latency are threaded to SearchField.tsx as two
  // SEPARATE values, not one pre-joined string, so it can render them as two
  // elements and degrade the detail first at a narrow width (see explorer.css's
  // container-query rules on `.listing-search-count-*`) instead of hiding the
  // whole pin — the count is the most useful part of it, so it should be the
  // last thing to go. `searchCountFull` (the title/aria-label sentence) is
  // unaffected — it is never truncated, only the VISIBLE chip is.
  let searchCountDetail: string | null = null;
  // The chip's reserved width covers a match count; the scan caveat makes it
  // longer, so the input reserves more while one is running.
  let widePin = false;
  if (showsSearchHits && searchState.status === "ok") {
    // A truncated rank (server per-query cap) means `hits` undercounts the
    // real tree. Signal that without new UI: a "+" on the number plus a
    // tooltip. Terse form for the chip, full sentence for title/aria. Past the
    // display cap the chip has to own up to it — "top 100 of 4.9K+" — because
    // the rendered list stops at the cap while the count keeps reporting the
    // whole ranking. The cap itself stays out of this file (result-cap.ts
    // owns it): `cappedAway` says whether it bit, `visibleHits` says how many
    // rows show.
    const suffix = searchState.truncated ? "+" : "";
    searchCount =
      cappedAway > 0
        ? `top ${visibleHits.length} of ${compact(hits.length)}${suffix}`
        : `${compact(hits.length)}${suffix} match${hits.length === 1 ? "" : "es"}`;
    searchCountFull = resultCountLabel(hits.length, searchState.truncated, mode);
  }

  // --- index scan caveat ----------------------------------------------------
  // Folded into the status chip rather than added beside it: the chip is
  // absolutely pinned inside the input, so a second element in that row would
  // have to compete with it for the same few pixels on a narrow pane. Both
  // facts are about the same search, and one line says both. Which message
  // appears is a claim about how far the results can be trusted, so it lives
  // in a pure, tested helper (listing/index-caveat).
  // `pending` is the listing's `requestComing`: an answer is on its way —
  // scheduled, or already in flight — which is not the same claim as the
  // rows being stuck (listing/index-caveat).
  const caveat = showsSearchHits
    ? searchCaveat(indexScan, { behind, pending: requestComing, rescanPending, failed: requestFailed })
    : null;
  if (caveat) {
    searchCountDetail = caveat.note;
    searchCountFull = caveat.title;
    widePin = true;
  } else if (searchState.status === "ok" && searchCount !== null) {
    // Decision 10: the latency readout home's box already shows beside its
    // own count (formatElapsed, home-search.ts) — reused rather than
    // reimplemented so the two boxes speak the same units. Only alongside a
    // SETTLED count, and only once nothing above has already folded a
    // caveat into the chip: `behind` (a stale count) always produces one
    // (index-caveat.ts), so this branch only runs when the count is both
    // present and current — a stale count paired with a fresh latency
    // figure would describe two different requests.
    const elapsed = formatElapsed(searchState.elapsedMs);
    searchCountDetail = elapsed;
    searchCountFull = `${searchCountFull} · ${elapsed}`;
    widePin = true;
  }

  // Is anything pinned inside the search input right now? Mirrors the chip
  // conditions in the render below; drives the input's right padding, so an
  // idle box gives its whole width to the placeholder. `searchCountDetail`
  // is checked on its own, not just alongside `searchCount` (code review,
  // 2026-09-10): a caveat with no settled count yet — "indexing…", "search
  // failed" — still pins the chip, so the padding has to reserve room for
  // it even though `searchCount` itself is still null.
  const hasPin =
    (searching && spinner) ||
    searchCount !== null ||
    searchCountDetail !== null;

  // The status strip's inputs. A search hit carries no size (the comment on
  // its row explains why), so the byte sum is only ever taken over the plain
  // listing — statusLine's own "searching" branch never reads either number.
  //
  // ITEM 11 (running-screen review, 2026-09-10) folded ITEM 6 into this
  // same predicate, so there is now exactly one reason for `showsSearchHits`
  // to appear down here rather than the two half-reasons an earlier
  // `showsSearchFooter` alias used to carry: it decides whether search hits
  // (sizeless) or the folder's own rows (sized) are what the strip is
  // summing. `statusLine`'s own "searching" branch handles the rest —
  // ITEM 6's fix (a gated, uncommitted query is NOT `showsSearchHits`, so it
  // falls back to the folder's own honest item count) and ITEM 11's fix (a
  // search with nothing selected returns `null` — no line, not "0 matches"
  // — since the box's own pinned chip already reports the match count and
  // this strip's only remaining job is the SELECTION the chip says nothing
  // about) both live in that one function, not here.
  let selectedBytes = 0;
  let selectedFolders = 0;
  if (!showsSearchHits) {
    for (const entry of sortedEntries) {
      if (!selectedSet.has(base + "/" + entry.name)) continue;
      if (entry.is_dir) selectedFolders++;
      else selectedBytes += entry.size ?? 0;
    }
  }
  const statusText = statusLine({
    total: sortedEntries.length,
    selected: sel.paths.length,
    selectedBytes,
    folderCount: selectedFolders,
    truncated: state.status === "ok" && state.truncated,
    searching: showsSearchHits,
    // The CAPPED, on-screen count (`visibleHits`, result-cap.ts's capHits),
    // not the raw match total (`hits.length`): a selection can only ever
    // include rows the body actually rendered, and the search box's own
    // pinned chip already owns up to the "top 100 of 4.9K" split when the
    // cap bites (searchCount above) — this line must not restate the bigger,
    // unreachable number as the selection's denominator.
    visibleHits: visibleHits.length,
  });

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
          {/* The one piece of chrome `_snapshot` adds: everywhere else the
              state is invisible by design (the plan's decisions log), but a
              listing has no per-row "as of" heading the way a content pane's
              template does, so silently showing a frozen tree with no
              explanation would read as a bug, not a feature. */}
          {inSnapshot && resolvedSnapshot && (
            <div className="listing-snapshot-banner">
              Showing this folder as of commit{" "}
              <span className="listing-snapshot-sha">
                {shortSha(resolvedSnapshot.sha)}
              </span>
              .
              <button
                type="button"
                className="listing-snapshot-back"
                onClick={backToLive}
              >
                Back to live
              </button>
            </div>
          )}
          {inSearchSlot(
            barSearchSlot,
            /* `searching` (a non-empty query) is what tells the crumb bar to
               stand the crumbs down and give the row its whole width — see
               #breadcrumb:has(.listing-search.searching) in explorer.css.
               Nothing to hand upward: the row is portaled INTO the bar, so a
               class on the row is already inside the bar's subtree.
               SearchField owns the box's own markup; this Listing supplies
               its OWN search state (this is the folder host — it answers
               the rows below with the same query) and the row's trailing
               chrome (the pane reopener, the kebab) as children, which a
               file host's own SearchField call has none of. */
            <SearchField
              active={ownsBarChrome}
              searchInputRef={searchInputRef}
              fsPath={fsPath}
              home={home}
              query={query}
              q={q}
              setQuery={setQuery}
              searching={searching}
              isPathQuery={isPathQuery}
              committed={showsSearchHits}
              awaitingCommit={!gateOpen}
              commitSearch={commitSearch}
              prefetchIndex={prefetchIndex}
              typedAddress={typedAddress}
              completion={completion}
              spinner={spinner}
              searchCount={searchCount}
              searchCountFull={searchCountFull}
              searchCountDetail={searchCountDetail}
              hasPin={hasPin}
              widePin={widePin}
            >
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
              {/* THE KEBAB (EntryActionsMenu), the folder's app-level one-shots:
                  App Doctor, Download app, Open as project — gated on the folder
                  having an entry page (`appEntryPath`: it IS an app) — and MCP
                  config, gated on the folder publishing a manifest. Whether or not
                  the pane is open: this row is the folder's own chrome, and the
                  pane's strip is the tab strip alone. "Open in project" used to
                  stand here as a bordered button while the pane was shut and in the
                  pane's strip while it was open; one kebab in one place replaces
                  both copies. A folder that qualifies for none of the rows gets no
                  `⋮` at all (the menu renders nothing on an empty list). Not on a
                  snapshot or a panel pane (`paneEnabled`), where the companions are
                  off too.

                  The entry page is what the rows act on (export's `entry_html`),
                  with `<folder>/index.html` as a stand-in when there is none so
                  the folder is still what the menu resolves. */}
              {(paneEnabled || ownsBarChrome) && (
                <EntryActionsMenu
                  /* The server's answer is os.path.abspath — backslashes on
                     Windows — and the menu derives the folder with a "/" split,
                     so it goes through the same drive-letter-only normalisation
                     the file surface applies before comparing. */
                  fsPath={appEntryPath ? canonEntryPath(appEntryPath) : fsPath + "/index.html"}
                  isEntry={paneEnabled && appEntryPath !== null}
                  mcp={
                    paneEnabled
                      ? {
                          available: mcpSrc !== null,
                          pending: folderMcp.pending,
                          reason: unavailableReason("mcp"),
                        }
                      : undefined
                  }
                  onOpenMcp={() => setMcpOpen(true)}
                  /* Open in embed — this listing under the chrome-free embed
                     prefix, in a new tab, `_mode=_listing` stamped so the embed
                     shows the LISTING rather than hopping to the folder's app
                     entry (the same stamp the file preview's row writes). Only
                     where this listing owns the bar: a panel pane or a snapshot
                     is not a page of its own to open. */
                  onOpenEmbed={
                    ownsBarChrome
                      ? () => {
                          const search = location.search;
                          const stamped = new URLSearchParams(search).has("_mode")
                            ? search
                            : (search ? search + "&" : "?") + "_mode=_listing";
                          window.open(embedUrlForFsPath(fsPath, stamped), "_blank", "noopener");
                        }
                      : undefined
                  }
                  /* The folder's own actions (lib/bar-menus' folderBarMenu via
                     `barMenu()`) — what the column header's `⋮` used to open.
                     Rebuilt per render, which is how Paste's enabled state
                     tracks the clipboard. Submenu rows have no home in this
                     flat menu; the folder menu has none. */
                  extraItems={
                    ownsBarChrome
                      ? barMenu().flatMap((e): OverflowEntry[] =>
                          e === "separator"
                            ? [e]
                            : e.submenu
                              ? []
                              : [{
                                  label: e.label,
                                  icon: e.icon,
                                  disabled: e.disabled,
                                  onClick: e.onClick ?? (() => {}),
                                }]
                        )
                      : undefined
                  }
                />
              )}
              {pane.on && !sideState.open && (
                <SideToggleButton what={modeTitle(paneSide)} onClick={openSide} />
              )}
              {/* The path `···` is not here any more: it rides the crumb strip
                  now (Breadcrumb.tsx), immediately right of the folder name it
                  acts on, which is one home instead of this row's and the
                  file view's. */}
            </SearchField>,
          )}
          <div
            ref={scrollRef}
            /* Two different dims, two different claims. `listing-stale` means
               an answer is on its way (the deferred render lags a keystroke,
               or the fetch/scan is mid-flight). `listing-behind` means the
               opposite: no answer is coming, these results are a generation
               old and staying that way until a boundary (listing/revalidate).
               The second can last the whole session, so it is deliberately
               the lighter of the two — it has to be legible to read under,
               not merely noticeable. */
            className={
              "listing-scroll" +
              (isStale ? " listing-stale" : "") +
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
               the one from before this press. A press on a row's icon+name
               handle, or anywhere on an already-selected row, drags; everything
               else sweeps; a press that barely moves is still the click it
               always was. */
            onPointerDownCapture={onListingPointerDownCapture}
            /* Bubble phase, so the marquee's capture snapshot above runs
               first. Deselecting on the CLICK instead is a trap — see
               onBackgroundPointerDown. */
            onPointerDown={onBackgroundPointerDown}
            onContextMenu={openBackgroundMenu}
          >
            <table className="listing-table">
              {/* Over an empty folder the column LABELS hide (visibility, see
                  .listing-head-empty); the strip itself stays so the table
                  keeps its shape under the "Empty directory" message. */}
              <thead className={emptyDir ? "listing-head-empty" : undefined}>
                <tr>
                  {showsSearchHits ? (
                    // One column, and NOT a sort control. Results are in
                    // relevance (fuzzy-rank) order, full stop: the hit set is
                    // capped and, while the walk streams, partial — ordering
                    // that by name or date presents it as an answer it isn't,
                    // and the search box already says the coverage is
                    // approximate (listing/index-caveat).
                    //
                    // The header names the base the shown relative paths are
                    // rooted at — the search field portaled into the crumb
                    // bar replaced the crumbs that used to answer that —
                    // falling back to the bare label if the base isn't known.
                    (() => {
                      // The base is a path, not a label: NAME and MODIFIED read as
                      // chrome in caps, but a Linux path is case-sensitive, so
                      // uppercasing it would show a path ("/HOME/IAMSDAS") that
                      // doesn't exist. Only "Path in " inherits the header's
                      // uppercase transform; the path itself is exempted below.
                      // Home contracts to a lone "~" here even though the three
                      // crumb-strip sites keep showing home's full path for a
                      // resting bar (contractHome's own contract) — in this
                      // header, home is the base most in need of shortening.
                      const baseText = searchBase === home ? "~" : contractHome(searchBase, home);
                      const baseLabel = searchBase ? `Path in ${baseText}` : "";
                      return (
                        <th className="col-name col-search-base" title={baseLabel || undefined}>
                          {searchBase ? (
                            <>
                              Path in <span className="col-search-base-path">{baseText}</span>
                            </>
                          ) : (
                            "Path"
                          )}
                        </th>
                      );
                    })()
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
                              LABEL without unmounting the strip (explorer.css,
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
                        </th>
                      ),
                    )
                  )}
                </tr>
              </thead>
              <tbody>{body}</tbody>
            </table>
          </div>
          {/* Spans the list column only, never the preview pane beside it —
              it sits INSIDE .listing-main, after the scroller, the same way
              the crumb slot sits inside it before. statusLine decides the
              string, INCLUDING whether there is one at all (`null` — ITEM
              11 — while search hits are showing and nothing is selected,
              since the box's own pinned chip already reports the match
              count); this only renders it.

              Gated on the folder having an actual answer — loaded
              (`state.status === "ok"`) or a search in flight or done —
              because `sortedEntries` is `[]` for every other state (still
              loading, failed, access denied) and an ungated footer would
              read "Empty folder" for a folder the app has not read yet.

              `showsSearchHits`, not raw `searching` (finding 4, code
              review): a path-shaped query is `searching` but never gets an
              answer (`isPathQuery` suppresses the rank request by design),
              so `showsSearchHits` is false for it and `statusText` falls
              through to the non-searching branch, keyed on `sortedEntries`
              — exactly the case this comment already warns about. Gating on
              raw `searching` let a path-shaped query slip past this check
              while the folder was still loading or had errored, showing
              "Empty folder" for a folder that was never actually read. */}
          {(state.status === "ok" || showsSearchHits) && statusText !== null && (
            <footer className="listing-status" title={statusText}>
              {statusText}
            </footer>
          )}
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
              {/* Keyed on WHAT THE PANE IS ABOUT (pane-side's paneKey): the mode
                  and the OPEN FOLDER, nothing else (D460). Every mode's subject is
                  this folder now, so the key changes only when the folder or the
                  mode does — never when the selection moves — which is what stops
                  arrow-keying down the listing remounting the chat/git/mcp iframe
                  (a `git status`/`git log` fork, or a second `agent.py` spawn) on
                  every keystroke.

                  `claudeAskInstance` rides along ONLY for `claude` (see its own
                  comment above): a second "Fix with AI" ask on the same folder
                  changes neither `paneSide` nor `fsPath`, so without it the key
                  would not change either, and the claude template's next boot
                  would never fire to pull the new prompt. */}
              <ListingPreviewPane
                key={paneSide === "claude"
                  ? `${paneKey(paneSide, fsPath)}:${
                      // ONLY A REAL `false` TAKES THE LEGACY SHAPE. Read as a
                      // boolean this walked `claudeAskInstance` (legacy, flag not
                      // yet read) → the delivery seq (flag landed on) → the seq
                      // again (delivered): the middle step remounted and booted a
                      // whole chat only to throw it away. So "not asked yet"
                      // takes the NATIVE shape — the one it keeps if the flag
                      // lands on — and a later `false` changes the key while
                      // `ChatMount` is still showing nothing but its cover
                      // (Preview.tsx `claudeMountKey` carries the same argument).
                      nativeChatState === false
                        ? claudeAskInstance
                        : askDelivery
                          ? askDelivery.seq
                          : 0
                    }`
                  : paneKey(paneSide, fsPath)}
                undecided={paneUndecided}
                folder={fsPath}
                side={paneSide}
                sideEntries={sideEntries}
                onSelectSide={selectSide}
                onClose={closeSide}
                initialAsk={paneSide === "claude" ? nativeAsk : null}
              />
            </div>
          </>
        )}
      </div>

      {/* The MCP companion's dialog, off the kebab (McpDialog). `mcpSrc` is
          re-read here rather than trusted from the click. */}
      {mcpOpen && mcpSrc && (
        <McpDialog src={mcpSrc} folderName={basename(base)} onClose={() => setMcpOpen(false)} />
      )}
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
