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
import { resolveFolderToOpen } from "@apps/explorer/listing/enter-prompt";
import { isPristineQuery } from "@apps/explorer/listing/query-pristine";
import { contractHome } from "@apps/explorer/listing/home-path";
import { useWidthThresholdRef } from "@apps/explorer/listing/search-hint-width";
import { searchSlot, subscribeSearchSlot } from "@apps/explorer/search-slot";
import { iconForEntry } from "@platform/ui/FileIcons";
import { BookmarkStar } from "@apps/explorer/Breadcrumb";

// The search button's own instant tooltip (platform/lib/hints.ts): the
// grammar this box understands, one rule per line.
//
// Each line leads with something TYPABLE rather than a description of a
// category — "*.pdf" is a thing to copy, "a pattern" is a thing to decode —
// and the four lines are the four answers `resolve_query`
// (fused_render/index/query.py) actually gives, in the order a person meets
// them.
//
// The depth rule is the one worth spelling out, because it reads backwards
// to anyone who assumes a shell: a glob with no "/" anywhere in it gets an
// implicit "**/" prefix server-side and matches at ANY depth, and it is the
// LEADING slash that pins a pattern to this folder alone (`resolve_query`'s
// own "/*.csv has to mean depth 1 under the box root").
//
// `hints.ts` splits a caption whose every line is `token — meaning` into an
// aligned two-column grid, so the `\n`s and the em dashes below are both
// load-bearing: they are the row and column separators.
// The magnifier, drawn once for the two places that need it: the collapsed
// (icon-only) form of the search button, and the dropdown's search-action
// row, whose icon slot has to be filled by SOMETHING or the row's label
// hangs a slot to the left of every folder name under it.
function SearchGlyph(): JSX.Element {
  return (
    <svg
      width="13"
      height="13"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="11" cy="11" r="7" />
      <line x1="16.5" y1="16.5" x2="21" y2="21" />
    </svg>
  );
}

const SEARCH_GRAMMAR_HINT =
  "report — names containing it, at any depth below\n" +
  "*.pdf — a pattern, at any depth below\n" +
  "/*.pdf — a pattern, in this folder only\n" +
  "~/Work/*.md — start from another folder";

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
  /**
   * FINDING 5 (code review, 2026-09-10): the SAME deferred, trimmed value
   * `awaitingCommit` (below) is computed from (`useListingSearch.ts`'s own
   * `q`) — not `query` above, which echoes every keystroke immediately.
   * Mixing a live `query`/`pristine` with a deferred `awaitingCommit` left
   * `pristine` (and the text `searchAffordance` reads) one render out of
   * step with the gate it's paired with: clearing a gated query down to a
   * plain word left the old, shape-based version of this value true for
   * one extra render while the box had already moved on, showing a stale
   * offer over results already live. `query` above stays live — the
   * `<input>`'s own `value` and every interaction handler still need the
   * immediate echo — this is only for the affordance calculation, which
   * needs to agree with `awaitingCommit` about which render it's
   * describing.
   */
  q: string;
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
  /**
   * ITEM 9 (running-screen review, 2026-09-10): whether THIS gated query's
   * commit gate has NOT yet been satisfied — `!gateOpen`
   * (useListingSearch.ts) — fed to the dropdown's search-offer predicate
   * instead of a text-shape check. The offer row promises "pressing this
   * changes what's on screen"; a promise about STATE (has this run yet)
   * has to be answered by state, not by shape (does this text escape the
   * folder), because shape stays true forever — `escapesFsPath`-derived
   * `gated` kept re-breaking this offer for exactly that reason (offering
   * over already-live results, one render out of step with `escapes`
   * itself, and — the running-screen review that renamed this — still
   * offering after the user had already pressed Enter and gotten their
   * 31 matches). `gateOpen` flips true the moment `commitSearch()` runs
   * for this exact text, so this value does too, with no second commit
   * tracker of its own.
   */
  awaitingCommit: boolean;
  commitSearch: () => void;
  prefetchIndex: () => void;
  typedAddress: TypedAddress;
  completion: Completion;
  spinner: boolean;
  searchCount: string | null;
  searchCountFull: string | undefined;
  /**
   * The least-actionable tail of the pin — the scan caveat ("not
   * refreshed") or the search latency ("119 ms") — split out from
   * `searchCount` so it can be dropped on its own at a narrow box width
   * without taking the match count down with it. `searchCountFull` (the
   * title/aria-label sentence) always carries the whole thing regardless of
   * what the visible chip can currently fit — see the pin-degradation
   * container queries in explorer.css (`.listing-search-count-detail`,
   * `.listing-search-count-base`). The query TEXT has priority over this
   * pin (running-screen defect, 2026-09-10): the pin gives way, never the
   * query, and this split is what lets it give way one rung at a time
   * instead of all at once.
   */
  searchCountDetail: string | null;
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
  q,
  setQuery,
  searching,
  isPathQuery,
  committed,
  awaitingCommit,
  commitSearch,
  prefetchIndex,
  typedAddress,
  completion,
  spinner,
  searchCount,
  searchCountFull,
  searchCountDetail,
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
  const HINT_LONG = "Search, or type a path or pattern to search elsewhere";
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
  //
  // Sets `fieldActive` false directly, the same way `navigateToCompletion`
  // above already does, rather than relying on the `onBlur` handler to get
  // there — the clear button's own onMouseDown calls `preventDefault()` (to
  // stop the browser's native mousedown-blur from firing before the click
  // completes), so without this the field would stay focused (and
  // `showCompletion` gated on `fieldActive` would stay live) through the
  // clear. Also blurs the input explicitly, which is required anyway since
  // a focused element does not un-focus itself just because its own state
  // says it should. This lands on the
  // resting, pre-filled state (an EMPTY query with the field unfocused —
  // `PathCrumbs` renders the folder's own path here, not this field's
  // value) and does not fight `onFocus`'s own pre-fill: `onFocus` only
  // seeds the query when it finds one already empty, which this leaves it.
  const clearSearchQuery = () => {
    setQuery("");
    setPinnedOpen(false);
    setFieldActive(false);
    searchInputRef.current?.blur();
  };
  // Tab and a row's own mousedown both COMPLETE TEXT: write the row's path
  // into the field so the dropdown re-keys on the new directory, without
  // navigating.
  const acceptCompletion = (item: CompletionItem) => {
    setQuery(item.path);
    searchInputRef.current?.focus();
  };
  // The field's own resting state: empty, or still exactly the folder path
  // the box pre-filled itself with on focus (`onFocus` below,
  // `contractHome(fsPath, home)`) — query-pristine.ts's `isPristineQuery`.
  // Nothing has been TYPED in either case, even though the box's own value
  // is non-empty in the second — the distinction `searchAffordance` below
  // and the completion exclusion need (SPEC-omnibox-search-affordance.md
  // correction, 2026-09-10).
  //
  // FINDING 5 (code review, 2026-09-10): checked against `q` (the deferred,
  // trimmed value), not the live `query` above — the same reasoning
  // FileSearchField.tsx's own pristine guard already applies to its
  // navigation effect, applied here to keep this in step with `escapes`
  // (see the `q` prop's own doc comment).
  const pristine = isPristineQuery(q, fsPath, home);

  // SPEC-omnibox-search-affordance.md scope item 4 (variant E): the ONE
  // pressable search offer the dropdown gets, plus the non-interactive
  // not-found notice above it for an unresolvable path-shaped query.
  // `searchAffordance` reads the SAME `isPathQuery`/`typedAddress`/
  // `searching` this field already has — not a second, parallel notion of
  // "is this a path" (the hard constraint the spec calls out by name) —
  // plus `awaitingCommit` (ITEM 9, running-screen review, 2026-09-10; this
  // parameter was called `gated` and read `escapes` until then): a STATE
  // read off the caller's own commit gate (useListingSearch.ts's
  // `!gateOpen`), not a text-shape check. `escapes` alone stays true for as
  // long as the text starts with `~/`, including after the commit that
  // satisfies it — three separate regressions traced back to exactly that
  // mismatch (offering over already-live results; a one-render skew
  // against the deferred `escapes` FINDING 5 fixed below used to read
  // instead; and finally, offering a row that would do nothing because the
  // search it promises had already run). `awaitingCommit` answers "would
  // pressing this row change what's on screen" directly, so it can't drift
  // from that promise the way a shape check already has three times.
  //
  // FINDING 5 (code review, 2026-09-10): reads `q`, not `query` —
  // `awaitingCommit` (like `escapes` before it) is already computed off `q`
  // by the caller, and passing the live `query` text alongside it let the
  // two disagree about which render they were describing (see the `q`
  // prop's own doc comment above).
  //
  // FINDING 3 (code review, 2026-09-10): also reads whether the completion
  // dropdown already has a real match for this text — a live completion
  // means the query is mid-typed toward something real, not a dead end, so
  // the not-found report has nothing true left to say.
  const hasCompletions = completion.items.length > 0;
  const affordance = searchAffordance(
    q,
    isPathQuery,
    typedAddress,
    searching,
    awaitingCommit,
    pristine,
    hasCompletions,
  );
  const hasAction = affordance.action !== null;
  // Pressing the action row: a `commitInPlace` row NAVIGATES to the folder
  // its own label names (ITEM 10, running-screen review, 2026-09-10) — a
  // path-shaped query that does not resolve rewrites the box to a plain
  // word instead (see search-action-rows.ts's own comment on
  // `commitInPlace` for why a second commit path for THAT case would be a
  // no-op — `isPathQuery` suppresses the rank request no matter how many
  // times commitSearch() runs).
  //
  // ITEM 10: `commitInPlace: true` used to mean "call `commitSearch()`",
  // which runs the search but never actually opens the folder the row's
  // own label promises ("Press Enter to open ~ and search") — the
  // breadcrumb, the URL and the search-hit rows' own relative paths then
  // all disagreed about where the search actually ran. The search is
  // already effectively rooted at `resolveFolderToOpen`'s folder (that's
  // what made this query "gated" in the first place), so opening it is
  // what makes those three agree, and it's what the retired banner
  // promised all along. `navigate(..., { q: action.query })` carries the
  // UNCHANGED query text along (`navHintQCommitted`, router.ts) so the
  // destination's own gate opens immediately rather than asking for a
  // second Enter — the same mechanism FileSearchField.tsx's own file-to-
  // folder hand-off already uses, not a second one grown for this case.
  // Falls back to the old in-place commit only when `home` has not
  // resolved yet (`resolveFolderToOpen` returns null) — nothing to
  // navigate to yet, so committing in place is still better than doing
  // nothing.
  const runAction = (action: SearchActionRow) => {
    if (action.commitInPlace) {
      const folder = resolveFolderToOpen(action.query, home);
      if (folder !== null) {
        navigate(folder, { isDir: true, q: action.query });
        return;
      }
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
  // `&& !pristine`: a pristine, pre-filled path resolves to a real folder,
  // so this would otherwise legitimately be true for it (the completion
  // machinery happily offers that folder's own children) — but nothing has
  // been TYPED into a pristine box, so there is nothing yet to complete;
  // the dropdown only opens once an actual edit gives it something to
  // answer.
  const showCompletion =
    fieldActive &&
    !pristine &&
    (hasAction ||
      (completion.target !== null &&
        completion.items.length > 0 &&
        !isExactSingleMatch(completion.items, completion.target)));
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
              // `clearSearchQuery` itself blurs now (ITEM 2 fix, above) —
              // Escape and the clear button share the exact same teardown,
              // not two copies of "clear, then remember to also blur."
              clearSearchQuery();
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
            //
            // FINDING 1 (code review, 2026-09-10): Tab with NOTHING
            // explicitly arrowed to must still complete the first REAL
            // completion, not run the action row sitting at index 0 — a
            // shell-completion Tab is a "finish typing this" gesture, and
            // the action row is not text to finish typing into the field.
            // Only when there IS no real completion to fall back to (the
            // action row is the only row on offer) does an un-arrowed Tab
            // still run it, matching what it already did before actions
            // existed at all.
            const tabDefaultIndex =
              hasAction && completion.items.length > 0 ? actionRowCount : 0;
            const action = completionKeyAction(
              e.key,
              showCompletion,
              highlight,
              totalRows,
              tabDefaultIndex,
            );
            if (action.type === "move") {
              e.preventDefault();
              // The dropdown is open and navigable: this arrow belongs to it
              // alone. Without stopPropagation, the SAME keypress also
              // reaches useListingSelection's document-level keydown
              // listener (bubble phase, registered on `document`), which
              // moves the file listing's selection behind the dropdown —
              // one keypress, two things moving at once. When the dropdown
              // is CLOSED, completionKeyAction returns "none" for arrows
              // (see its own showCompletion gate) and this branch is never
              // reached, so the event bubbles untouched and the listing
              // still navigates from the search box exactly as before.
              e.stopPropagation();
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
            // ITEM 10 (running-screen review, 2026-09-10): a bare Enter on a
            // gated, non-path query — nothing explicitly arrowed to, so
            // `completionKeyAction` fell through to `enter-passthrough`
            // above — gets the SAME treatment the action row does: navigate
            // to the folder this query is actually rooted at, carrying the
            // unchanged query text, rather than calling `commitSearch()`
            // and leaving the folder unopened. `awaitingCommit`, not
            // `escapes`: once the gate has already opened for this exact
            // text (a second Enter, or arriving here with nothing left to
            // do), there is nothing to navigate to — `escapes` alone would
            // still fire and re-navigate to the same folder for no reason.
            if (awaitingCommit) {
              const folder = resolveFolderToOpen(query.trim(), home);
              if (folder !== null) {
                e.preventDefault();
                navigate(folder, { isDir: true, q: query });
                return;
              }
              e.preventDefault();
              commitSearch();
            }
          }}
        />
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
                  <span className="listing-completion-icon">
                    <SearchGlyph />
                  </span>
                  <span className="listing-completion-name">{affordance.action.label}</span>
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
                    <span className="listing-completion-icon">
                      {iconForEntry(item.name, item.is_dir)}
                    </span>
                    <span className="listing-completion-name">{item.name}</span>
                    {/* A folder shows no size, exactly as it does in the
                        listing table below (Listing.tsx's `td.size`). The
                        word "folder" used to sit here instead, which left
                        one column carrying two different kinds of fact —
                        a TYPE on some rows and a SIZE on others — so it
                        answered no single question. The icon states the
                        type now, and this column is only ever a size. */}
                    <span className="listing-completion-hint">
                      {item.is_dir ? "" : formatSize(item.size)}
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
          // The query text has priority over this pin (running-screen
          // defect, 2026-09-10): "31 matches · not refreshed" was clipping
          // a committed query down to a handful of visible characters
          // because the pin's fixed reservation never gave ground. It is
          // split into two elements, not one, so explorer.css's container
          // queries on THIS box's own inline-size can drop the least
          // actionable part first — the detail (timing/caveat), then the
          // count itself — while the reserved input padding shrinks to
          // match at each rung. `title`/`aria-label` stay on the OUTER span
          // and always carry the full sentence, so the caveat this can
          // degrade away visually is still reachable as a tooltip even once
          // nothing is left on screen — see explorer.css's 360px rule,
          // which keeps this element a small hoverable target rather than
          // removing it.
          <span
            className="listing-search-count"
            title={searchCountFull}
            aria-label={searchCountFull}
          >
            <span className="listing-search-count-base">{searchCount}</span>
            {searchCountDetail !== null && (
              <span className="listing-search-count-detail"> · {searchCountDetail}</span>
            )}
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
            rather than a second path to the same open-and-focus behaviour.
            The SEED differs, and that is the whole difference between the
            two gestures: ⌘L is the location bar and opens holding the
            current address, this button says "Search" and opens empty,
            because a search box that opens holding the folder you are
            already looking at has to be cleared before it can be used.

            `data-hint` carries the box's own grammar (SEARCH_GRAMMAR_HINT,
            above) rather than a native `title` — `hints.ts` is this app's
            one instant, un-clippable tooltip, and a native title would
            double up with it (both firing over the same point). The
            accessible name stays on `aria-label` below; the hint is a
            sighted-hover affordance only, read by nothing else. */}
        {!pinnedOpen && !hasClear && (
          <button
            type="button"
            className={"listing-search-shortcut-hint bar-ctl" + (boxWide ? "" : " bar-ctl-icon")}
            data-hint={SEARCH_GRAMMAR_HINT}
            // The accessible name carries the shortcut in BOTH forms — in
            // the collapsed (icon-only) form this is the ONLY place it
            // still appears at all, so it is load-bearing there, not just
            // a duplicate of visible text.
            aria-label={`Search this folder (${isMac ? "⌘L" : "Ctrl L"})`}
            onClick={() => requestSearchFocus("")}
          >
            {boxWide ? (
              <>
                {"Search"}
                <kbd>{isMac ? "⌘L" : "Ctrl L"}</kbd>
              </>
            ) : (
              <SearchGlyph />
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
