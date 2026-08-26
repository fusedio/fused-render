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
let pending: { path: string; prompt: string } | null = null;

export function stageClaudeAsk(path: string, prompt: string): void {
  pending = { path, prompt };
}

/**
 * Read-and-clear, but only when `path` matches the surface asking. A surface
 * for a DIFFERENT path must not consume (and thereby lose) an ask that is
 * still waiting for its own target — the folder-pane's own child surfaces
 * and the file sidebar both call this on every mount, for whatever path they
 * each are, and only the one that matches may have it.
 */
export function takePendingClaudeAsk(path: string): string | null {
  if (!pending || pending.path !== path) return null;
  const { prompt } = pending;
  pending = null;
  return prompt;
}

// Test-only: production code never needs to look without taking.
export function peekPendingClaudeAsk(): { path: string; prompt: string } | null {
  return pending;
}
