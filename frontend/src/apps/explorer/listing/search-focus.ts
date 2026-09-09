// Decision 1: once a folder's crumb bar is claimed, the merged search field
// (Listing.tsx) is the one path affordance left in the bar — Breadcrumb.tsx's
// own click-to-edit and Ctrl/Cmd+L no longer open a second, path-only editor
// over it. They ask this field to focus instead.
type Listener = () => void;
let listeners: Listener[] = [];

export function requestSearchFocus(): void {
  for (const l of listeners) l();
}

export function subscribeSearchFocusRequest(fn: Listener): () => void {
  listeners = [...listeners, fn];
  return () => {
    listeners = listeners.filter((l) => l !== fn);
  };
}
