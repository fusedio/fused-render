// The file view's half of the merged field (SearchField.tsx) — mounted over
// every plain file that owns the explorer's crumb bar (Preview.tsx, gated the
// same way `usePreviewFileMenu`'s own `ownsBar` is: `actionsInTopbar &&
// !stat.is_dir`).
//
// No complex logic: this box does not search. It carries the same query
// state SearchField needs (`useListingSearch`, `useTypedPathAddress`,
// `useCompletion` — the identical hooks the folder view calls, aimed at the
// file's PARENT folder instead of the file itself) purely to drive the box's
// own interaction — the completion dropdown, Enter, the commit gate — and the
// moment that gate opens (Enter for an escaping path, or immediately for a
// plain filter/glob that never leaves the folder), it navigates to the parent
// with the query attached (`navigate`'s `opts.q`) and stops. The parent
// folder's own Listing is what actually searches, seeded already-committed
// (`navHintQCommitted`, router.ts) so it never asks for a second Enter.
//
// `useListingSearch(parentPath, home, 0, false)` — the trailing `false` is
// `urlSync`: this box's query is never mirrored onto the file's own URL (that
// belongs to no view here), and the effect below fires before the hook's own
// fetch ever would (a `useLayoutEffect`, ahead of the hook's passive-effect
// request), so no rank request goes out against the parent while this file's
// page is still the one on screen.
import { useLayoutEffect, useRef, useSyncExternalStore } from "react";
import { navigate } from "@platform/lib/router";
import { dirname } from "@apps/explorer/lib/fs-actions";
import { useListingSearch } from "@apps/explorer/listing/useListingSearch";
import { useTypedPathAddress } from "@apps/explorer/listing/useTypedPathAddress";
import { useCompletion } from "@apps/explorer/listing/useCompletion";
import { showingSearchHits } from "@apps/explorer/listing/search-body-mode";
import { claimFolderChrome } from "@apps/explorer/listing/folder-chrome";
import { useHome } from "@apps/explorer/listing/home-path";
import { isPristineQuery } from "@apps/explorer/listing/query-pristine";
import { searchSlot, subscribeSearchSlot, inSearchSlot } from "@apps/explorer/search-slot";
import { SearchField } from "@apps/explorer/SearchField";

export interface FileSearchFieldProps {
  // Whether this file owns the explorer's crumb bar — `usePreviewFileMenu`'s
  // own `ownsBar` (`actionsInTopbar && !stat.is_dir`), threaded down rather
  // than recomputed so the two can never disagree about which file view is
  // "the" explorer one.
  active: boolean;
  fsPath: string;
}

export function FileSearchField({ active, fsPath }: FileSearchFieldProps) {
  const parentPath = dirname(fsPath);

  // The same `home` Listing.tsx's folder box resolves — `useHome`
  // (home-path.ts) caches the one `/api/config` lookup across both, so a
  // file opened after its own folder has already been browsed gets the
  // answer on this box's very first render instead of a second round trip.
  const home = useHome();

  const searchInputRef = useRef<HTMLInputElement>(null);

  const {
    query,
    setQuery,
    q,
    searching,
    isPathQuery,
    escapes,
    gateOpen,
    commitSearch,
    prefetchIndex,
    searchState,
    awaitingCommit,
  } = useListingSearch(parentPath, home, 0, false);
  const typedAddress = useTypedPathAddress(query, parentPath, home);
  const completion = useCompletion(query, parentPath, home);
  const committed = showingSearchHits(searchState, awaitingCommit);

  // The claim itself: this file's bar behaves like a claimed folder's (the
  // field replaces the path's click-to-edit, the star moves inside its
  // border) without relocating anything — no slot, so the bar stays exactly
  // where it always rendered (Preview.tsx has no split column for it to move
  // into; that layout change is a folder-only concern, D-something in
  // folder-chrome.ts).
  useLayoutEffect(() => {
    if (!active) return;
    return claimFolderChrome(null);
  }, [active]);

  // The gate this box answers is exactly Decision 4's (useListingSearch):
  // `escapes` false means the query never leaves the box root, so `gateOpen`
  // is already true the moment it is long enough to search at all
  // (`searching`); an escaping query (one whose base is genuinely a
  // different folder — `escapesFsPath`, query-base.ts) waits here the same
  // way the folder view's own box would, for an explicit Enter via
  // `commitSearch`. Either way, once open, this page's own job is
  // finished — hand the query to the parent and let its Listing take it from
  // there.
  //
  // Landing on `gateOpen` (not `committed`, which additionally waits for an
  // actual answer) is what keeps this a layout effect that fires before any
  // request goes out: `gateOpen` flips the instant the query is a plain
  // filter/glob or Enter has run, with no round trip in between.
  //
  // `!isPristineQuery(q, ...)` (SPEC-omnibox-search-affordance.md
  // correction, 2026-09-10) — a hard requirement, not an optimisation: this
  // box always arrives pre-filled with `parentPath`'s own absolute path
  // (SearchField.tsx's `onFocus`), and since `escapesFsPath` no longer
  // treats a same-subtree absolute path as escaping, that untouched
  // pre-fill alone now satisfies `searching && gateOpen` the MOMENT the
  // field is focused — before the user has typed a single character.
  // Without this guard, focusing a file's search box would immediately hand
  // off to the parent folder with the pre-filled path as its "committed"
  // query, which is not a search anyone asked for.
  //
  // Checked against `q` (the SAME deferred value `escapes`/`gateOpen`
  // themselves are computed from), not the live `query`: right after a
  // keystroke, `query` has already moved but `q` can still be sitting on
  // the pristine seed for one more render — checking the live value here
  // would let exactly that stale-`q` render slip through as "not pristine"
  // while `gateOpen` was actually still answering for the seed.
  const firedRef = useRef(false);
  useLayoutEffect(() => {
    if (!active || firedRef.current) return;
    if (!searching || !gateOpen) return;
    if (isPristineQuery(q, parentPath, home)) return;
    firedRef.current = true;
    navigate(parentPath, { isDir: true, q: query });
  });

  // Same target Listing.tsx's folder box portals into (search-slot.ts) —
  // published only once `claimFolderChrome` above has actually run
  // (Breadcrumb.tsx's `FolderSearchSlot`), so before that first layout effect
  // commits this reads null and the box renders inline, same as it would with
  // no crumb bar at all.
  const barSlot = useSyncExternalStore(subscribeSearchSlot, searchSlot, () => null);

  // Every hook above runs unconditionally (Rules of Hooks); only the render
  // itself is gated. `!active` is every surface that isn't the explorer's own
  // file view — an embed, a panel pane's preview, anything without a bar to
  // take over — where this field renders nowhere rather than inline (it would
  // have nothing published to portal into anyway, but a stray inline copy is
  // exactly the "second copy of the field" this file exists to avoid).
  if (!active) return null;

  return inSearchSlot(
    barSlot,
    <SearchField
      active={active}
      searchInputRef={searchInputRef}
      fsPath={parentPath}
      crumbsFsPath={fsPath}
      home={home}
      query={query}
      q={q}
      setQuery={setQuery}
      searching={searching}
      isPathQuery={isPathQuery}
      committed={committed}
      escapes={escapes}
      commitSearch={commitSearch}
      prefetchIndex={prefetchIndex}
      typedAddress={typedAddress}
      completion={completion}
      spinner={false}
      searchCount={null}
      searchCountFull={undefined}
      hasPin={false}
      widePin={false}
      // SPEC-omnibox-search-affordance.md correction (2026-09-10): a file
      // view keeps no listing of its parent folder's own entries — nothing
      // honest to build an example from, so the teaching panel simply shows
      // none here rather than falling back to an invented one.
      entries={[]}
    />,
  );
}
