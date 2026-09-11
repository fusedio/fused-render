// Right-click on the merged field's box (Listing.tsx's `.listing-search-box`,
// searchBoxRef) over a claimed folder. The field is a text input, so the
// browser's own context menu (Copy/Paste/spelling) is the useful one whenever
// there is text — or a caret — to act on; the bar menu (New File, Paste,
// Refresh, ...) only belongs on the right-click when the box is showing
// nothing but the resting breadcrumb strip behind it.
//
// "Resting" is not a new notion: it is the exact condition Listing.tsx
// already gates `.listing-search-crumbs` on (`query === "" && !pinnedOpen`,
// see PathCrumbs' one call site) — reused here rather than reinvented, so
// this handler and that render can never disagree about what "resting"
// means. Pulled out as its own predicate (rather than inlined in the
// onContextMenu prop) so the stand-down condition is checkable without
// mounting the component: a focused-but-empty box (`pinnedOpen`, no query
// yet) and a box with a query typed both keep the native menu, because both
// are places the user pastes into — a glob pattern into the query, a path
// into the box before it commits.
export function searchBoxRestingForContextMenu(query: string, pinnedOpen: boolean): boolean {
  return query === "" && !pinnedOpen;
}
