// The crumb bar's header-actions portal target, published as a store.
//
// Breadcrumb.tsx renders the `#topbar-mode-slot` div; Preview.tsx portals the
// view's mode switcher and primary action into it. That used to be a
// `getElementById` in a mount effect, which held only because the two were
// siblings rendered in the same commit.
//
// They aren't any more: over a folder the whole crumb bar portals down into
// the listing's left column (listing/folder-chrome.ts), and changing a
// portal's container rebuilds the subtree — a new slot node. A reference
// captured at mount would then point at a detached div and the mode switcher
// would simply vanish. Publishing the live node instead makes the portal
// follow the bar wherever it goes, with no assumption about which effect runs
// first.
let slot: HTMLElement | null = null;
const subscribers = new Set<() => void>();

function notify(): void {
  for (const fn of subscribers) fn();
}

export function publishTopbarSlot(el: HTMLElement): void {
  if (slot === el) return;
  slot = el;
  notify();
}

// Identity-checked: during a relocation the outgoing slot may be torn down
// after the incoming one has already published, and an unconditional clear
// there would blank a live bar.
export function retractTopbarSlot(el: HTMLElement | null): void {
  if (slot !== el) return;
  slot = null;
  notify();
}

export function topbarSlot(): HTMLElement | null {
  return slot;
}

export function subscribeTopbarSlot(fn: () => void): () => void {
  subscribers.add(fn);
  return () => {
    subscribers.delete(fn);
  };
}

// Test seam: the store outlives any component.
export function resetTopbarSlot(): void {
  slot = null;
  notify();
}
