// The crumb bar's SEARCH portal target, published as a store.
//
// Over a folder the left column used to carry two full-height strips: the
// crumb bar, and a search row directly under it holding the box, the sort chip
// and the path `···`. The pane on the other side of the divider has one. Two
// against one read as a mistake, so the search row moved INTO the bar: crumbs
// at the left, the box and the `···` at its right end, one strip per column.
//
// Breadcrumb.tsx renders the target div (only while a folder holds the chrome
// claim — a file view's bar has no search); Listing.tsx portals its existing
// `.listing-search` row into it, unchanged. A portal rather than moving the
// markup, because the row is woven into the listing's state: the query, the
// walk's live counts, the selection readout and `searchInputRef` (which the
// keyboard focuses from anywhere in the listing) all belong to Listing.
//
// Hosts with no crumb bar — the app builder — publish nothing, and the row
// renders where it always did, as the column's own first strip.
import { createNodeSlot } from "@apps/explorer/node-slot";

const slot = createNodeSlot();

export const publishSearchSlot = slot.publish;
export const retractSearchSlot = slot.retract;
export const searchSlot = slot.get;
export const subscribeSearchSlot = slot.subscribe;
export const resetSearchSlot = slot.reset;
