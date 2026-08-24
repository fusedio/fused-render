// The host side of the "Fix with AI" pull (review #804 round 2). Both
// Preview.tsx (the file sidebar) and Listing.tsx (the folder pane) install
// `window._fusedClaudeAskTake` — the claude template's own boot calls it
// through the runtime's ancestor hop (static/runtime.js `pullClaudeAsk`) to
// collect whatever prompt is waiting, if any.
//
// This replaces the round-1 design: a `_fused_ask` query param baked into the
// claude iframe's `src`, kept "one-shot" by a cache keyed on "has the
// ask-less base changed". That shape had a hole no cache design closed — ANY
// remount of the iframe for a reason that had nothing to do with a new ask
// (toggling the sidebar away and back, closing and reopening the pane)
// rebuilt the identical cached src and replayed a stale error into a
// brand-new conversation, because a `src` is an ADDRESS and "follow this part
// of it only the first time" cannot be expressed by a URL, however it is
// cached.
//
// A PULL closes that gap structurally rather than by caching harder: the
// prompt is plain in-memory state on the host, and `takeClaudeAsk` is the
// one and only way to read it — reading it IS clearing it, in the same step,
// so there is no separate "and now remember this was already sent" bookkeeping
// to get wrong. Whatever frame actually boots and calls this exactly once
// gets the text; every OTHER boot — a remount with nothing new pending, two
// pulls in a row, a boot before any ask ever arrived — gets `null`. There is
// no "is this the same key as last time" question left to ask, because the
// consumption already happened the first time anything asked.
export function takeClaudeAsk(pending: { current: string | null }): string | null {
  const value = pending.current;
  pending.current = null;
  return value;
}
