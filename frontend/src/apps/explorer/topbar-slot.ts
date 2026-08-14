// The crumb bar's header-actions portal target, published as a store.
//
// Breadcrumb.tsx renders the `#topbar-mode-slot` div; Preview.tsx portals the
// view's mode switcher and primary action into it. Why a published node rather
// than a `getElementById` at mount — the bar relocates over a folder — is
// written down once, in node-slot.ts.
import { createNodeSlot } from "@apps/explorer/node-slot";

const slot = createNodeSlot();

export const publishTopbarSlot = slot.publish;
export const retractTopbarSlot = slot.retract;
export const topbarSlot = slot.get;
export const subscribeTopbarSlot = slot.subscribe;
export const resetTopbarSlot = slot.reset;
