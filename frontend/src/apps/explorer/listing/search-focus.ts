// Decision 1: once a folder's crumb bar is claimed, the merged search field
// (Listing.tsx) is the one path affordance left in the bar — Breadcrumb.tsx's
// own click-to-edit and Ctrl/Cmd+L no longer open a second, path-only editor
// over it. They ask this field to focus instead.
//
// The merged field is BOTH a location bar and a search box, and the two
// gestures want different openings. Ctrl/Cmd+L and click-to-edit are the
// location-bar gesture: they seed the "~"-contracted current address,
// selected, so it can be replaced by typing or copied immediately. The
// button labelled "Search" is the other one, and a search box that opens
// holding the folder you are already looking at asks you to clear it before
// you can use it — that caller passes "".
//
// `seed` stays required rather than optional so an empty opening is always a
// caller SAYING empty, never a caller forgetting the address.
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
