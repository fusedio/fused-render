// The preview sidebar's portal target, published as a store.
//
// The sidebar is a PAGE-LEVEL column: it stands beside the crumb bar and the
// content together, running the full height of the window, so its header row is
// the top of its own column rather than something under a bar that spans the
// window. That makes it a sibling of the whole left column — and the left column
// is `#breadcrumb` + `#content`, which StatView (shell/App.tsx) owns.
//
// Preview, which is the thing that KNOWS whether there is a sidebar and what is
// in it, renders several levels below that. So StatView renders the slot and
// Preview portals into it. Same arrangement, in the other direction, as the crumb
// bar's own `#topbar-mode-slot`: the container and the content are owned by
// different components, and a published node (rather than a getElementById at
// mount) is what makes the portal survive the container being rebuilt — see
// node-slot.ts.
import { createNodeSlot } from "@apps/explorer/node-slot";

const slot = createNodeSlot();

export const publishPreviewSideSlot = slot.publish;
export const retractPreviewSideSlot = slot.retract;
export const previewSideSlot = slot.get;
export const subscribePreviewSideSlot = slot.subscribe;
export const resetPreviewSideSlot = slot.reset;
