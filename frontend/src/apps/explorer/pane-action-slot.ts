// The preview pane header's PRIMARY-ACTION portal target, published as a store.
//
// The open folder's "Open as app" (lib/app-button) belongs to the whole view,
// so Preview.tsx builds it — but the title bar is a bad place to show it. That
// bar also holds the crumbs and, since the search row moved up into it
// (search-slot.ts), the search box and the `···`; a labelled pill wedged among
// them squeezed the path down to "create-a-…" on a folder whose name is the
// one thing the bar is for.
//
// The pane header across the divider has room and almost nothing in it. So the
// button renders there instead — same button, same action, no second copy.
//
// Preview.tsx owns the CONTENT and ListingPreviewPane owns the CONTAINER, with
// a Listing between them, so this is the only sane channel: the container
// publishes its node, the content portals into whatever is published. The pane
// is not always there (below the split's width threshold it does not render at
// all), and `null` is exactly the signal Preview needs to keep the button in
// the title bar in that case — a folder must not lose its primary action just
// because the window got narrow.
import { createNodeSlot } from "@apps/explorer/node-slot";

const slot = createNodeSlot();

export const publishPaneActionSlot = slot.publish;
export const retractPaneActionSlot = slot.retract;
export const paneActionSlot = slot.get;
export const subscribePaneActionSlot = slot.subscribe;
export const resetPaneActionSlot = slot.reset;
