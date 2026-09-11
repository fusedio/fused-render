// The crumb bar's SEARCH portal target, published as a store.
//
// Over a folder the left column used to carry two full-height strips: the
// crumb bar, and a search row directly under it holding the box, the sort chip
// and the path `···`. The pane on the other side of the divider has one. Two
// against one read as a mistake, so the search row moved INTO the bar: crumbs
// at the left, the box and the `···` at its right end, one strip per column.
//
// Breadcrumb.tsx renders the target div (only while the bar holds the chrome
// claim — Listing.tsx over a folder, FileSearchField.tsx over a plain file);
// the claimant portals its own SearchField into it, unchanged. A portal
// rather than moving the markup, because the row is woven into the caller's
// own state: the query, the walk's live counts and `searchInputRef` (which
// the keyboard focuses from anywhere in the view) belong to whichever of the
// two claimed the bar.
//
// Hosts with no crumb bar — the app builder — publish nothing, and the row
// renders where it always did, as the column's own first strip.
import type { ReactNode } from "react";
import { createPortal } from "react-dom";
import { createNodeSlot } from "@apps/explorer/node-slot";

const slot = createNodeSlot();

export const publishSearchSlot = slot.publish;
export const retractSearchSlot = slot.retract;
export const searchSlot = slot.get;
export const subscribeSearchSlot = slot.subscribe;
export const resetSearchSlot = slot.reset;

// Portals `row` into the slot when one is published, or hands it back as-is
// otherwise (a host with no crumb bar at all). One helper so a claimant's own
// render never has to spell out the `slot ? createPortal(...) : row` branch.
export function inSearchSlot(slot: HTMLElement | null, row: ReactNode): ReactNode {
  return slot ? createPortal(row, slot) : row;
}
