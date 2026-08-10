// A published DOM node, as a store: "here is the live element to portal into".
//
// The explorer's crumb bar hosts two portals whose CONTENT is rendered by a
// different component than the one that owns the container — the view's mode
// control (topbar-slot.ts) and, over a folder, the listing's search row
// (search-slot.ts). Both used to be `getElementById` in a mount effect, which
// held only while container and content were siblings committed together.
//
// They aren't: over a folder the whole bar portals down into the listing's
// left column (listing/folder-chrome.ts), and changing a portal's container
// REBUILDS the subtree — a new slot node. A reference captured at mount would
// then point at a detached div and the portaled content would simply vanish.
// Publishing the live node makes the portal follow the bar wherever it goes,
// with no assumption about which effect runs first.
export type NodeSlot = {
  publish: (el: HTMLElement) => void;
  retract: (el: HTMLElement | null) => void;
  get: () => HTMLElement | null;
  subscribe: (fn: () => void) => () => void;
  // Test seam: the store outlives any component.
  reset: () => void;
};

export function createNodeSlot(): NodeSlot {
  let slot: HTMLElement | null = null;
  const subscribers = new Set<() => void>();

  const notify = (): void => {
    for (const fn of subscribers) fn();
  };

  return {
    publish(el) {
      if (slot === el) return;
      slot = el;
      notify();
    },
    // Identity-checked: during a relocation the outgoing slot may be torn down
    // after the incoming one has already published, and an unconditional clear
    // there would blank a live bar.
    retract(el) {
      if (slot !== el) return;
      slot = null;
      notify();
    },
    get() {
      return slot;
    },
    subscribe(fn) {
      subscribers.add(fn);
      return () => {
        subscribers.delete(fn);
      };
    },
    reset() {
      slot = null;
      notify();
    },
  };
}
