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

// --- honesty (review #804 round 3, findings 1/3/4/6) -----------------------
//
// `window._fusedAskClaude`'s return value has to mean "claude will actually
// be shown", not "a callback happens to exist" (finding 4) — the runtime's
// `noteAskClaude` is a transparent conduit for whatever this returns. Getting
// that right turns out to also be what closes finding 3 (a seed that can
// never be delivered must never be STORED in the first place, so there is
// nothing left to go stale) and finding 1 (a target with no sidebar at all —
// a directory opened at `?_mode=git`, Preview's main body — still has a real
// route to claude, through the ordinary content-mode switch rather than the
// `_side` split).
//
// `claudeEntryReady` is the one fact every caller needs the same way: an
// entry is only "ready" when it EXISTS and its gate has SETTLED — a pending
// verdict (CT-12) is not a "no", but it is not a "yes" either, and answering
// `true` for it would store a seed nothing is about to pull (finding 3's
// exact hole) while answering `false` for it costs nothing but one narrow,
// self-correcting false negative (the same click, or the next one, succeeds
// once the gate lands).
export function claudeEntryReady(
  entry: { mode: string } | null | undefined,
  pending: boolean
): boolean {
  return !!entry && entry.mode === "claude" && !pending;
}

// Where an ask should go, given what THIS RENDER already knows: the file
// sidebar's split (`side`) when the surface has one, else the folder-less
// content pane's own mode switch (`content`) — Preview.tsx's only route when
// `splitCapable` is false (no file preview to sit beside), which is exactly
// the shape a directory opened at `?_mode=git` has (finding 1). `null` means
// "not ready anywhere, right now" — the honest answer `_fusedAskClaude`
// reports back through the ancestor hop as `false` (finding 4), and the one
// answer that guarantees nothing gets stored for later (finding 3).
//
// Pure: the caller has already reduced its own render's state to two
// booleans, which is what makes this testable with no React tree, no
// `stat`, no `conditions` map — only the question that matters.
export type ClaudeAskRoute = "side" | "content" | null;

export function resolveClaudeAskRoute(opts: {
  splitCapable: boolean;
  sideReady: boolean;
  contentReady: boolean;
}): ClaudeAskRoute {
  if (opts.splitCapable) return opts.sideReady ? "side" : null;
  return opts.contentReady ? "content" : null;
}
