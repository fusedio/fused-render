// The git sidebar's "Fix with AI" one-shot prompt, consumed correctly.
//
// Two hosts hand a pending seed to a Claude iframe: Preview.tsx (one
// persistent sidebar iframe whose `src` is rebuilt every render) and
// Listing.tsx (a companion pane that remounts on `paneKey(side, folder)`
// changing). Both tried "clear the seed in an effect keyed on leaving
// `claude`" first, and both were wrong for the same reason: staying ON
// `claude` while the SUBJECT changes underneath it (a different file, a
// different folder) never fires an effect keyed only on the mode, so the old
// seed survived into a src/mount that has nothing to do with the error it was
// written for — re-sending a stale git error into a conversation about an
// unrelated file or folder.
//
// The fix is to make CONSUMPTION the thing that clears the seed, keyed to the
// specific target (`key`) a render is building a Claude src/mount for — an
// invariant checked on every call, not a side effect that has to remember to
// fire at the right time. `key` is whatever uniquely identifies "the Claude
// document this render would produce" for the caller: Preview.tsx uses the
// ask-less base of the iframe src it is about to build (the target file,
// `_remote`, `chat_only`); Listing.tsx uses `paneKey(side, folder)`.
//
// Pure and DOM-free like this module's siblings (preview-rev.ts,
// preview-side.ts), so the property that matters — "a seed is delivered to at
// most one key, exactly once, and repeated calls for that SAME key keep
// answering the SAME thing" — is a fact about two refs and a string, testable
// with no React tree, no iframe, and no window global in sight.
export interface ClaudeAskCache {
  key: string;
  seed: string | null;
}

// `pending`/`cache` are the CALLER's own refs (mutated in place — this is not
// a React hook, just the decision a hook's render makes each time it runs):
//
//   pending  the raw ref `window._fusedClaudeAsk`'s installed callback writes
//            into. Non-null exactly when a NEW ask has arrived and not yet
//            been resolved for any key.
//   cache    what the LAST call resolved, and for which key — so a call for
//            the same key with nothing new pending returns the identical
//            answer it gave last time (idempotent: rebuilding a src from that
//            answer produces the identical string, which is what stops an
//            unrelated re-render from reloading an iframe that already has
//            the seed it needs).
//
// Three cases, and `resolveClaudeAskSeed` does not need to know which one it
// is in — the two comparisons cover all three:
//   - same key, nothing new pending: return the cached answer unchanged.
//   - same key, a NEW ask just arrived (a second "Fix with AI" click on the
//     same target): resolve and cache the new value — a deliberate second
//     request, not a stray re-render.
//   - a different key (the target changed, or this is the first call ever):
//     resolve whatever is pending NOW (usually null, unless a seed that
//     belonged to the OLD key was never delivered — which cannot happen once
//     callers consume it as documented, but resolving here rather than
//     silently carrying it forward is what makes that a property of this
//     function rather than of callers remembering to clear something).
export function resolveClaudeAskSeed(
  pending: { current: string | null },
  cache: { current: ClaudeAskCache | null },
  key: string
): string | null {
  const cached = cache.current;
  if (cached && cached.key === key && pending.current === null) {
    return cached.seed;
  }
  const seed = pending.current;
  pending.current = null;
  cache.current = { key, seed };
  return seed;
}
