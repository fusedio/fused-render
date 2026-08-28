// A "Fix with Claude" ask staged from OUTSIDE the target surface entirely —
// the repo-updates row in the activity card (shell/RepoUpdatesDock.tsx,
// SPEC §36), which has no chat of its own and is not even mounted in the
// same tree as the folder it is about. It stages `{path, prompt}` here, then
// navigates to `path`; whichever Preview/Listing surface mounts for that
// path next picks the ask up and delivers it through the SAME
// `claudeAskActionRef` mechanism a click inside that surface's own git
// companion would use (claude-ask.ts).
//
// This is deliberately a SEPARATE store from that file's `takeClaudeAsk`,
// not a second caller of it: `takeClaudeAsk` reads a per-COMPONENT-INSTANCE
// ref that only exists once a target surface's Claude sidebar is already
// about to boot for a prompt raised from INSIDE it — there is no such ref to
// write into before that surface has even mounted. This module is the one
// piece of state that survives the gap between "stage it" and "something
// mounts to claim it".
//
// READ-AND-CLEARED IN ONE STEP, matching the guarantee `takeClaudeAsk`
// documents (claude-ask.ts:26-30): whichever surface asks for `path` first
// gets the prompt, and the read IS the consumption, so there is no separate
// "already delivered" bookkeeping to get wrong. A second stage before the
// first is claimed simply overwrites the one slot — there is only ever one
// pending cross-navigation ask at a time, matching there being only one
// activity card and one button press at a time.
//
// A URL PARAM WAS REJECTED for the same reason claude-ask.ts's own header
// rejects one for its pull: the prompt holds a git error and repo state
// verbatim (arbitrarily long, arbitrary characters), and a bookmarked or
// reloaded URL replaying a stale error into a brand-new conversation is
// exactly the bug that shape reintroduces — worse here, since the ask would
// ride along on every navigation to that folder from then on rather than
// just the one that raised it.
//
// EXPIRES ON A TIMEOUT, for the same class of bug one more way: if the
// navigated-to surface never reaches a ready Claude route (Claude Code not
// installed, the gate refuses, the user navigates somewhere else first and
// only comes back to this folder later), an un-expiring slot would sit here
// and get delivered to that LATER, unrelated visit — a stale error replayed
// into a brand-new conversation, the exact failure this module's header
// already rejects a URL-carried ask over. `PENDING_TTL_MS` is generous
// enough to cover the real navigate-then-mount-then-gate-settles window
// (seconds, not the width of a whole session) without giving a truly stale
// ask a second life.
const PENDING_TTL_MS = 60_000;

let pending: { path: string; prompt: string; stagedAt: number } | null = null;

// A stage NOTIFIES, on top of writing `pending` (Bugbot finding 17b, code
// review 2026-08-27). Both consumers of this module — Listing.tsx and
// Preview.tsx — used to pull the staged prompt from an effect keyed only on
// `[fsPath, claudeReady]` (or Preview's `[fsPath, claudeAskRoute,
// suppressForListing]`). That misses the COMMON case: the user is usually
// looking at the very repo whose card just failed, so the explorer is
// already sitting on `path` with Claude already ready when the second stage
// lands. `navigate(path)` is then a no-op — neither dep changes — so the
// effect never re-runs and the prompt sits in `pending` until it expires,
// unseen. `stagedVersion` is a monotonic counter every stage bumps
// (including a second stage for the SAME path), and `subscribePendingClaudeAsk`
// lets each consumer's `useSyncExternalStore` add it to their own dependency
// array alongside their existing deps — so a stage while already mounted
// re-runs the effect even when nothing else changed. This does not replace
// path-matching: `takePendingClaudeAsk` below is still what decides whether
// THIS surface's path is the one being asked for; the version only decides
// WHEN to check again.
let stagedVersion = 0;
const listeners = new Set<() => void>();

export function subscribePendingClaudeAsk(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function pendingClaudeAskVersion(): number {
  return stagedVersion;
}

export function stageClaudeAsk(path: string, prompt: string): void {
  pending = { path, prompt, stagedAt: Date.now() };
  stagedVersion += 1;
  for (const listener of listeners) listener();
}

function liveOrExpired(): { path: string; prompt: string } | null {
  if (!pending) return null;
  if (Date.now() - pending.stagedAt > PENDING_TTL_MS) {
    pending = null;
    return null;
  }
  return pending;
}

/**
 * Read-and-clear, but only when `path` matches the surface asking AND the
 * ask hasn't expired (see `PENDING_TTL_MS` above). A surface for a
 * DIFFERENT path must not consume (and thereby lose) an ask that is still
 * waiting for its own target — the folder-pane's own child surfaces and the
 * file sidebar both call this on every mount, for whatever path they each
 * are, and only the one that matches may have it.
 */
export function takePendingClaudeAsk(path: string): string | null {
  const current = liveOrExpired();
  if (!current || current.path !== path) return null;
  pending = null;
  return current.prompt;
}

// Test-only: production code never needs to look without taking.
export function peekPendingClaudeAsk(): { path: string; prompt: string } | null {
  const current = liveOrExpired();
  return current ? { path: current.path, prompt: current.prompt } : null;
}
