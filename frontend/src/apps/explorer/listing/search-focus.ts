// Decision 1: once a folder's crumb bar is claimed, the merged search field
// (Listing.tsx) is the one path affordance left in the bar — Breadcrumb.tsx's
// own click-to-edit and Ctrl/Cmd+L no longer open a second, path-only editor
// over it. They ask this field to focus instead.
//
// The merged field is this app's location bar over a claimed folder, and a
// location bar opens seeded with the current address, selected — type to
// replace, or copy immediately. Both callers open the same field over the
// same folder, so both hand it the same "~"-contracted current path: there
// is no caller left that wants the field to open empty, so `seed` is
// required rather than optional.
type Listener = (seed: string) => void;
let listeners: Listener[] = [];

export function requestSearchFocus(seed: string): void {
  for (const l of listeners) l(seed);
}

export function subscribeSearchFocusRequest(fn: Listener): () => void {
  listeners = [...listeners, fn];
  return () => {
    listeners = listeners.filter((l) => l !== fn);
  };
}
