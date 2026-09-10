// THE MERGED SEARCH FIELD — one box, rendered by whichever view currently
// claims the crumb bar's search row (folder-chrome.ts): a folder's own
// Listing, or a file's own Preview. Both hosts render this exact component
// with this exact JSX; the difference between them is entirely in what they
// pass it, never in a second copy of the markup.
//
// A folder host owns real search state — the query answers real rows it is
// about to show in its own body — so it keeps calling useListingSearch,
// useCompletion and useTypedPathAddress itself and hands the results down.
// A file host has no body of its own to answer: committing a query there
// navigates to the parent folder instead of showing anything in place (see
// FileSearchField.tsx), so its caller supplies the same shape of props built
// from the SAME hooks, called against the parent path, with the results-
// display props (spinner/count/pin) left inert since nothing here ever shows
// them before the navigation away.
//
// What stays LOCAL to this component, in both hosts, is the field's own
// interaction chrome: whether it is pinned open, which completion row is
// highlighted, whether the field is focused right now, and the measured
// width that picks the short or long placeholder. None of that answers
// anything about search RESULTS, so neither host needs it and duplicating it
// per host would be the drift this component exists to prevent.
import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
  type RefObject,
} from "react";
import { navigate } from "@platform/lib/router";
import { basename, formatSize } from "@platform/lib/format";
import { isMac } from "@platform/lib/platform";
import { PathCrumbs } from "@apps/explorer/listing/path-crumbs";
import { requestSearchFocus, subscribeSearchFocusRequest } from "@apps/explorer/listing/search-focus";
import { searchBoxRestingForContextMenu } from "@apps/explorer/listing/search-box-context-menu";
import { searchBoxBlurAction } from "@apps/explorer/listing/search-provisional";
import { openTopbarMenu } from "@apps/explorer/topbar-menu";
import { type TypedAddress } from "@apps/explorer/listing/useTypedPathAddress";
import { type Completion, type CompletionItem } from "@apps/explorer/listing/useCompletion";
import { completionKeyAction, moveHighlight } from "@apps/explorer/listing/completion-keys";
import { isExactSingleMatch } from "@apps/explorer/listing/completion-target";
import { searchAffordance, type SearchActionRow } from "@apps/explorer/listing/search-action-rows";
import { isPristineQuery } from "@apps/explorer/listing/query-pristine";
import { contractHome } from "@apps/explorer/listing/home-path";
import { useWidthThresholdRef } from "@apps/explorer/listing/search-hint-width";
import { SEARCH_EXAMPLES, showSearchExamples } from "@apps/explorer/listing/search-examples";
import { searchSlot, subscribeSearchSlot } from "@apps/explorer/search-slot";
import { BookmarkStar } from "@apps/explorer/Breadcrumb";

export interface SearchFieldProps {
  /** This host currently owns the crumb bar's search row. */
  active: boolean;
  /**
   * The `<input>` itself. Owned by the CALLER, not this component: a
   * folder's own useListingSelection/useListingShortcuts already hold this
   * ref to focus the field directly for type-to-search and Ctrl+F-style
   * shortcuts, before this component's own subscribeSearchFocusRequest
   * effect ever runs — one ref, so both paths agree on which node "the
   * search input" means. A file host with no such external consumer just
   * makes its own with `useRef` and passes it through unused elsewhere.
   */
  searchInputRef: RefObject<HTMLInputElement>;
  /** The folder a committed query searches/navigates against. */
  fsPath: string;
  /**
   * What the resting (empty, unfocused) crumbs show. Defaults to `fsPath` —
   * a folder's own resting crumbs are its own path. A file host passes its
   * own path here (ending in its own name) while `fsPath` stays the parent
   * folder that a query actually searches.
   */
  crumbsFsPath?: string;
  home: string | undefined;
  query: string;
  setQuery: (q: string) => void;
  searching: boolean;
  /**
   * Path-shaped and glob-free (path-shaped-query.ts's `isPathShapedQuery`) —
   * shape only, never existence. The same value gates whether the caller's
   * own `useListingSearch` ever issues a rank request at all, so the chip
   * and the actual search behaviour can never disagree.
   */
  isPathQuery: boolean;
  /**
   * Whether the CURRENT query has a committed, matching search behind it
   * (Listing.tsx's `showsSearchHits`, `showingSearchHits(searchState,
   * awaitingCommit)`). Blur's discard/unpin choice keys on this, not on the
   * looser `searching` (typed-but-uncommitted still discards) — passed as
   * its own prop instead of re-derived here so both hosts read one
   * definition of "committed" rather than each guessing at it.
   */
  committed: boolean;
  escapes: boolean;
  commitSearch: () => void;
  prefetchIndex: () => void;
  typedAddress: TypedAddress;
  completion: Completion;
  spinner: boolean;
  searchCount: string | null;
  searchCountFull: string | undefined;
  hasPin: boolean;
  widePin: boolean;
  /**
   * Row-level chrome that sits AFTER the box, inside the same `.listing-search`
   * strip that stands the crumbs down and takes the whole width once
   * `searching`/`pinnedOpen` say so (Listing.tsx's pane-reopen button and the
   * folder's own kebab menu). Not part of this component's own concern — a
   * folder's row and a file's differ here — so it is handed in as children
   * rather than grown into a second prop surface; a file host passes none.
   */
  children?: ReactNode;
}

export function SearchField({
  active,
  searchInputRef,
  fsPath,
  crumbsFsPath,
  home,
  query,
  setQuery,
  searching,
  isPathQuery,
  committed,
  escapes,
  commitSearch,
  prefetchIndex,
  typedAddress,
  completion,
  spinner,
  searchCount,
  searchCountFull,
  hasPin,
  widePin,
  children,
}: SearchFieldProps) {
  const crumbsPath = crumbsFsPath ?? fsPath;

  // The field's own mode chip: whether the box holds a path-shaped, glob-free
  // query (real folder path, a bare tilde, a partial prefix of one — shape
  // only, never existence: path-shaped-query.ts) or a real pending search.
  // `searching` already answers "is anything typed at all"; layered onto it,
  // `isPathQuery` is the one existing predicate for "this reads as a path" —
  // there is no second, parallel test for "is this a search" here, only
  // these two already-computed booleans, and this is the SAME value the
  // caller's own `useListingSearch` gates its rank request on, so the chip
  // and whether a search actually ran can never disagree.
  const chipIsSearch = searching && !isPathQuery;

  // Decision 1: the focused-and-empty hint's two variants — the full example
  // teaches the pattern syntax in the space it takes to read it, but a narrow
  // field would clip it mid-example, teaching the wrong thing. `boxWide`
  // tracks whether the field currently has room for the long form; measured
  // rather than a CSS breakpoint because the threshold is about THIS box's
  // width, not the window's.
  const [boxWide, setBoxWide] = useState(false);
  const HINT_LONG = "Search, or type a path or pattern like ~/work/*/*.csv";
  const HINT_SHORT = "Search, or type a path or pattern";
  const HINT_WIDE_PX = 340; // roughly what HINT_LONG needs at 13px not to clip
  const searchBoxRef = useWidthThresholdRef(HINT_WIDE_PX, setBoxWide);

  // Breadcrumb.tsx's click-to-edit and Ctrl/Cmd+L, once this view's bar is
  // claimed, ask this field to focus instead of opening a second path editor
  // over it. `requestSearchFocus` has no per-view target — it notifies every
  // subscriber — so an inactive SearchField (a preview pane's own embedded
  // listing, `active` false) must not act on it, or a click on the CLAIMED
  // bar's crumb would steal focus into the wrong field.
  const seedSelectRef = useRef(false);
  useEffect(() => {
    if (!active) return;
    return subscribeSearchFocusRequest((seed) => {
      setQuery(seed);
      seedSelectRef.current = true;
      setPinnedOpen(true);
      searchInputRef.current?.focus();
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active]);
  useEffect(() => {
    if (!seedSelectRef.current) return;
    seedSelectRef.current = false;
    searchInputRef.current?.select();
  }, [query]);

  // `pinnedOpen` is the user asking for the full-strip box (clicked the
  // magnifier, or focused it — it stays until it blurs empty, or until an
  // uncommitted query blurs with text in it at all — search-provisional.ts),
  // rendering `.expanded`. A non-empty query outranks it the same way:
  // `.searching` stands the crumbs down and takes the whole strip too.
  const [pinnedOpen, setPinnedOpen] = useState(false);

  // Enter on an explicitly highlighted completion row NAVIGATES into it — a
  // completed folder path is a destination, not just more text (unlike Tab's
  // `acceptCompletion` below).
  const navigateToCompletion = (item: CompletionItem) => {
    navigate(item.absPath, { isDir: item.is_dir });
    setQuery("");
    setPinnedOpen(false);
    setFieldActive(false);
    searchInputRef.current?.blur();
  };
  // The teardown Escape and the clear button both need — an uncommitted
  // query is discarded and the box stands down from its pinned-open state.
  const clearSearchQuery = () => {
    setQuery("");
    setPinnedOpen(false);
  };
  // Tab and a row's own mousedown both COMPLETE TEXT: write the row's path
  // into the field so the dropdown re-keys on the new directory, without
  // navigating.
  const acceptCompletion = (item: CompletionItem) => {
    setQuery(item.path);
    searchInputRef.current?.focus();
  };
  // A click on an example inserts it rather than searching it blind — the
  // point is to teach, so it leaves the user holding an editable query with
  // focus intact, same as `acceptCompletion`.
  const acceptExample = (pattern: string) => {
    setQuery(pattern);
    searchInputRef.current?.focus();
  };

  // The field's own resting state: empty, or still exactly the folder path
  // the box pre-filled itself with on focus (`onFocus` below,
  // `contractHome(fsPath, home)`) — query-pristine.ts's `isPristineQuery`.
  // Nothing has been TYPED in either case, even though the box's own value
  // is non-empty in the second — the distinction both the examples panel
  // and `searchAffordance` below need (SPEC-omnibox-search-affordance.md
  // correction, 2026-09-10).
  const pristine = isPristineQuery(query, fsPath, home);

  // SPEC-omnibox-search-affordance.md scope item 4 (variant E): the ONE
  // pressable search offer the dropdown gets, plus the non-interactive
  // not-found notice above it for an unresolvable path-shaped query.
  // `searchAffordance` reads the SAME `isPathQuery`/`typedAddress`/
  // `searching` this field already has — not a second, parallel notion of
  // "is this a path" (the hard constraint the spec calls out by name) —
  // plus `escapes` (correction, 2026-09-10): the SAME predicate the
  // caller's own commit gate reads (useListingSearch.ts's `escapesFsPath`),
  // so an already-live, ungated search is never offered a row that would
  // read as "nothing has happened yet" over results already on screen.
  const affordance = searchAffordance(query, isPathQuery, typedAddress, searching, escapes, pristine);
  const hasAction = affordance.action !== null;
  // Pressing the action row: a bare word commits the query exactly as Enter
  // already falls through to (decision 4's gate); a path-shaped query that
  // does not resolve rewrites the box to a plain word instead (see
  // search-action-rows.ts's own comment on `commitInPlace` for why a
  // second commit path for THAT case would be a no-op — `isPathQuery`
  // suppresses the rank request no matter how many times commitSearch()
  // runs).
  const runAction = (action: SearchActionRow) => {
    if (action.commitInPlace) {
      commitSearch();
    } else {
      setQuery(action.query);
    }
    searchInputRef.current?.focus();
  };

  const [highlight, setHighlight] = useState(-1);
  // The highlight tracks the CURRENT list by position, not by identity —
  // resets on every list change, to -1 (nothing highlighted), not 0. See
  // Listing.tsx's own history of this exact effect for why. `hasAction` is
  // part of "the list changed" too: the action row can appear or vanish
  // (typedAddress settling from "checking" to "missing", say) with neither
  // `completion.target?.dir` nor `completion.items.length` moving at all,
  // which would otherwise leave a stale highlight pointing at the wrong row
  // once the row it named shifts index.
  useEffect(() => {
    setHighlight(-1);
  }, [completion.target?.dir, completion.items.length, hasAction]);
  // Whether the field itself is the thing focused right now — distinct from
  // `pinnedOpen` above, which deliberately OUTLIVES a blur once there is a
  // query. The dropdown needs the opposite: it must close the moment focus
  // leaves.
  const [fieldActive, setFieldActive] = useState(false);
  // `&& !pristine`: SPEC correction, 2026-09-10. A pristine, pre-filled path
  // resolves to a real folder, so this would otherwise legitimately be true
  // for it (the completion machinery happily offers that folder's own
  // children) — but the teaching panel (`showExamples`, below) is what a
  // pristine box shows instead, and enforcing the exclusion HERE (rather
  // than trusting `showSearchExamples` to defer the other way) is what
  // keeps the two surfaces from ever both rendering by construction.
  const showCompletion =
    fieldActive &&
    !pristine &&
    (hasAction ||
      (completion.target !== null &&
        completion.items.length > 0 &&
        !isExactSingleMatch(completion.items, completion.target)));
  const showExamples = showSearchExamples(fieldActive, pristine);
  // The action row, when present, is always the FIRST row (index 0) — the
  // folder completions that follow it shift up by exactly this many slots.
  // One number, read everywhere an index has to cross that boundary, so the
  // arrow-key math and the render below can't drift out of sync with each
  // other about where the folder rows actually start.
  const actionRowCount = hasAction ? 1 : 0;
  const totalRows = actionRowCount + completion.items.length;

  const firstRowRef = useRef<HTMLDivElement>(null);
  const rowsRef = useRef<HTMLDivElement>(null);
  const [rowsMaxHeight, setRowsMaxHeight] = useState<number | undefined>(undefined);
  useLayoutEffect(() => {
    if (totalRows > 5 && firstRowRef.current) {
      setRowsMaxHeight(firstRowRef.current.offsetHeight * 5.5);
    } else {
      setRowsMaxHeight(undefined);
    }
  }, [totalRows, completion.target?.dir, fieldActive]);
  useLayoutEffect(() => {
    if (highlight < 0) return;
    const row = rowsRef.current?.querySelector<HTMLElement>(`[data-idx="${highlight}"]`);
    row?.scrollIntoView({ block: "nearest" });
  }, [highlight]);

  // The pin is a request to type: focus follows it in the same interaction.
  useEffect(() => {
    if (pinnedOpen) searchInputRef.current?.focus();
  }, [pinnedOpen]);

  // …the search row portals into the crumb bar's own slot once one is
  // published there (search-slot.ts) — non-null only once the bar has
  // rendered its target, which is only ever over a view that claimed the
  // chrome; a host with no crumb bar (the app builder) keeps the row in
  // place as its own first strip.
  const barSearchSlot = useSyncExternalStore(subscribeSearchSlot, searchSlot, () => null);

  const hasClear = query !== "";

  return (
    <div
      className={
        "listing-search" +
        (searching ? " searching" : "") +
        (pinnedOpen ? " expanded" : "")
      }
    >
      <div
        ref={searchBoxRef}
        className={
          "listing-search-box" +
          // No mode modifier here any more (SPEC-omnibox-search-affordance.md
          // scope item 2): `--chip-inset` collapsed to one value once both
          // modes render the same glyph-only chip width, so this element no
          // longer needs a per-mode class of its own to hang it from — only
          // `.listing-search-mode` below still carries `chipIsSearch` (its
          // colour, not layout, keys off it).
          (hasPin ? " has-pin" : "") +
          (widePin ? " wide-pin" : "") +
          (hasClear ? " has-clear" : "")
        }
        // Right-click restores the bar menu, but only while resting — the
        // same `query === "" && !pinnedOpen` PathCrumbs itself gates on
        // (searchBoxRestingForContextMenu), so this and the crumbs' own
        // visibility can never disagree. `openTopbarMenu` with no crumb
        // argument resolves whatever the CURRENT view published for itself
        // (topbar-menu.ts) — the open folder's menu over a folder, the open
        // file's own menu over a file — so this needs no host-specific
        // branch of its own.
        onContextMenu={(e) => {
          if (!searchBoxRestingForContextMenu(query, pinnedOpen)) return;
          if (!openTopbarMenu(e.clientX, e.clientY)) return;
          e.preventDefault();
        }}
      >
        <span
          className={"listing-search-mode" + (chipIsSearch ? " search" : "")}
          aria-hidden="true"
        >
          {chipIsSearch ? (
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="11" cy="11" r="7" />
              <line x1="16.5" y1="16.5" x2="21" y2="21" />
            </svg>
          ) : (
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z" />
            </svg>
          )}
          {/* SPEC-omnibox-search-affordance.md scope item 1: the visible word
              is gone — the glyph above carries the mode alone — but a
              screen reader still needs a spoken label, since there is no
              longer a visible word for it to read. */}
          <span className="sr-only">{chipIsSearch ? "Search" : "Path"}</span>
        </span>
        {/* Decision 1: one field, carrying either a path or a pattern. The
            resting crumbs are the HOST's own path — a folder's own, or a
            file's own ending in its own name — never the search scope
            (`fsPath`) when the two differ. */}
        {query === "" && !pinnedOpen && (
          <PathCrumbs fsPath={crumbsPath} home={home} />
        )}
        <input
          ref={searchInputRef}
          type="search"
          className="listing-search-input"
          placeholder={pinnedOpen ? (boxWide ? HINT_LONG : HINT_SHORT) : ""}
          value={query}
          onFocus={() => {
            if (query === "") {
              setQuery(contractHome(fsPath, home));
              seedSelectRef.current = true;
            }
            setPinnedOpen(true);
            setFieldActive(true);
            prefetchIndex();
          }}
          onBlur={() => {
            const action = searchBoxBlurAction(committed, query === "");
            if (action === "discard") {
              setQuery("");
              setPinnedOpen(false);
            } else if (action === "unpin") {
              setPinnedOpen(false);
            }
            setFieldActive(false);
          }}
          onChange={(e) => {
            setQuery(e.target.value);
          }}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              e.preventDefault();
              clearSearchQuery();
              e.currentTarget.blur();
              return;
            }
            // `completionKeyAction`/`moveHighlight` stay generic over a row
            // COUNT and an INDEX into it — they don't know or care what a
            // row IS, so folding the action row into the same index space
            // as the folder completions (`totalRows`, above) needs no
            // change to either. The action row is always index 0 when
            // present: this is the ONE place that index maps back to a row
            // to act on, matched by the ONE place the render below builds
            // the same mapping in the opposite direction.
            const action = completionKeyAction(
              e.key,
              showCompletion,
              highlight,
              totalRows,
            );
            if (action.type === "move") {
              e.preventDefault();
              setHighlight((h) => moveHighlight(h, action.delta, totalRows));
              return;
            }
            if (action.type === "tab-accept" || action.type === "enter-accept") {
              e.preventDefault();
              if (hasAction && action.index === 0) {
                runAction(affordance.action as SearchActionRow);
              } else {
                const item = completion.items[action.index - actionRowCount];
                if (action.type === "tab-accept") {
                  acceptCompletion(item);
                } else {
                  navigateToCompletion(item);
                }
              }
              return;
            }
            if (e.key !== "Enter") return;
            // Decision 5: Enter resolves the field three ways. A real folder
            // navigates; a real file navigates too. Anything else falls
            // through to committing the search (decision 4's gate).
            if (typedAddress.status === "exists") {
              e.preventDefault();
              navigate(typedAddress.path, { isDir: typedAddress.is_dir });
              return;
            }
            if (escapes) {
              e.preventDefault();
              commitSearch();
            }
          }}
        />
        {showExamples && (
          <div className="listing-completion listing-completion-examples" role="listbox">
            {/* SPEC-omnibox-search-affordance.md scope item 4: the guidance
                that used to live ONLY in the placeholder (invisible the
                instant anything is typed) is readable here instead, right
                alongside the examples it's introducing. The placeholder
                keeps carrying the same text too — it costs nothing and
                still serves the emptied-box case. */}
            <div className="listing-completion-row listing-completion-notice">
              {boxWide ? HINT_LONG : HINT_SHORT}
            </div>
            <div className="listing-completion-rows">
              {SEARCH_EXAMPLES.map((ex) => (
                <div
                  key={ex.pattern}
                  role="option"
                  aria-selected={false}
                  className="listing-completion-row listing-completion-example"
                  onMouseDown={(e) => {
                    e.preventDefault();
                    acceptExample(ex.pattern);
                  }}
                >
                  <span className="listing-completion-name">{ex.pattern}</span>
                  <span className="listing-completion-hint">{ex.hint}</span>
                </div>
              ))}
            </div>
          </div>
        )}
        {showCompletion && (
          <div className="listing-completion" role="listbox">
            {/* SPEC-omnibox-search-affordance.md scope item 4: a path-shaped
                query that does not resolve gets this warning line above the
                search offer below — non-interactive (no data-idx, no
                aria-selected: it is not a row the arrow keys ever land on),
                reusing the existing pathNotFoundMessage() text
                (search-action-rows.ts) rather than a rewritten string. */}
            {affordance.notice && (
              <div className="listing-completion-row listing-completion-notice">
                {affordance.notice}
              </div>
            )}
            <div
              className="listing-completion-rows"
              ref={rowsRef}
              style={rowsMaxHeight !== undefined ? { maxHeight: rowsMaxHeight } : undefined}
            >
              {/* The one search-offer row this dropdown ever shows, always
                  first (index 0) — reachable by arrow keys but never the
                  DEFAULT selection (`highlight` starts at -1, same as every
                  other row here), so a bare Enter on a path-shaped query
                  still resolves the path exactly as before and never lands
                  here by accident. */}
              {affordance.action && (
                <div
                  ref={firstRowRef}
                  data-idx={0}
                  role="option"
                  aria-selected={0 === highlight}
                  className={
                    "listing-completion-row listing-completion-action" +
                    (0 === highlight ? " highlight" : "")
                  }
                  onMouseDown={(e) => {
                    e.preventDefault();
                    runAction(affordance.action as SearchActionRow);
                  }}
                  onMouseEnter={() => setHighlight(0)}
                >
                  <span className="listing-completion-name">
                    Search this folder for &quot;{affordance.action.query}&quot;
                  </span>
                  <span className="listing-completion-hint">↵</span>
                </div>
              )}
              {completion.items.map((item, i) => {
                const idx = actionRowCount + i;
                return (
                  <div
                    key={item.path}
                    ref={idx === 0 ? firstRowRef : undefined}
                    data-idx={idx}
                    role="option"
                    aria-selected={idx === highlight}
                    className={
                      "listing-completion-row" +
                      (idx === highlight ? " highlight" : "")
                    }
                    onMouseDown={(e) => {
                      e.preventDefault();
                      acceptCompletion(item);
                    }}
                    onMouseEnter={() => setHighlight(idx)}
                  >
                    <span className="listing-completion-name">{item.name}</span>
                    <span className="listing-completion-hint">
                      {item.is_dir ? "folder" : formatSize(item.size)}
                    </span>
                  </div>
                );
              })}
            </div>
          </div>
        )}
        {searching && spinner && (
          <span className="listing-search-spinner" aria-hidden="true" />
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
        {hasClear && (
          <button
            type="button"
            className="listing-search-clear"
            aria-label="Clear search"
            onMouseDown={(e) => {
              e.preventDefault();
              clearSearchQuery();
            }}
          >
            <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true">
              <path
                d="M4 4l8 8M12 4l-8 8"
                stroke="currentColor"
                strokeWidth="1.4"
                strokeLinecap="round"
              />
            </svg>
          </button>
        )}
        {/* SPEC-omnibox-search-affordance.md scope item 3 (variant F),
            revised (user preference, on seeing both on a running screen):
            the words stay — "Search ⌘L" — the defect was never the WORDS,
            it was that they wore no chassis and caught no click. It is a
            real button now, on the same `bar-ctl` family the neighbouring
            `⋮` and `★` controls ride, with the words as its actual content
            rather than a bespoke outlined pill (that would just reproduce
            the original complaint: four things on one line, four
            disagreeing styles). This also removes the "two magnifiers"
            risk the spec flagged as its riskiest unverifiable detail —
            with the left chip icon-only (scope item 1) and this button
            carrying the word, only one magnifier glyph exists at all once
            `!boxWide` collapses this button to it.

            `boxWide` is the SAME measurement (`HINT_WIDE_PX`, `searchBoxRef`
            above) the placeholder's own long/short switch already uses —
            not a second breakpoint — collapsing this button to the bare
            glyph exactly where the field is too narrow for the words to
            fit without wrapping or clipping.

            `requestSearchFocus` is the exact call Breadcrumb.tsx's own
            ⌘L/Ctrl+L listener makes (listing/search-focus.ts) — reused
            rather than a second path to the same open-and-focus behaviour. */}
        {!pinnedOpen && !hasClear && (
          <button
            type="button"
            className={"listing-search-shortcut-hint bar-ctl" + (boxWide ? "" : " bar-ctl-icon")}
            title="Search this folder"
            // The accessible name carries the shortcut in BOTH forms — in
            // the collapsed (icon-only) form this is the ONLY place it
            // still appears at all, so it is load-bearing there, not just
            // a duplicate of visible text.
            aria-label={`Search this folder (${isMac ? "⌘L" : "Ctrl L"})`}
            onClick={() => requestSearchFocus(contractHome(crumbsPath, home))}
          >
            {boxWide ? (
              <>
                Search <kbd>{isMac ? "⌘L" : "Ctrl L"}</kbd>
              </>
            ) : (
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <circle cx="11" cy="11" r="7" />
                <line x1="16.5" y1="16.5" x2="21" y2="21" />
              </svg>
            )}
          </button>
        )}
        {/* The star, trailing the count/spinner pin, as the box's own last
            child — it sits inside the field's own border. Gated on
            `barSearchSlot`: this row IS the bar's search row only once it has
            portaled into a claimed crumb bar; the inline copy this component
            would otherwise render for a pane or a framed listing has no bar
            of its own to sit inside, so Breadcrumb.tsx keeps carrying the
            star for those. */}
        {barSearchSlot && (
          <BookmarkStar id="bookmark-btn" name={basename(crumbsPath)} />
        )}
      </div>
      {children}
    </div>
  );
}
