// The Git door's words and dots (ClaudeRail's Git door, claude-rail GitPeek),
// pure — what the flag-off rail's Git section says in its hints, said once
// here so the door, its card and the old section cannot drift.
import type { RailGitRepo } from "@platform/lib/api";

/** THE CHECK'S DOT, always on while it has something to say (owner,
 *  2026-11-07: "the green dot, yellow dot and red dot we have for git should
 *  still pop up no matter what"). Red > yellow > green:
 *    attention (red)   the last check did not reach the remote;
 *    running (yellow)  a check is out;
 *    unread (green)    the remote's default branch has commits this one lacks.
 *  A re-check after a failed one stays red until it answers (the card's
 *  button says "Checking…" meanwhile).
 *  Null: nothing to say. */
export function gitChipDot(s: {
  checking: boolean;
  checked: boolean | null;
  behind: number | null | undefined;
}): "attention" | "running" | "unread" | null {
  if (s.checked === false) return "attention";
  if (s.checking) return "running";
  if ((s.behind ?? 0) > 0) return "unread";
  return null;
}

/** WHAT A PUSH WOULD MOVE (owner, 2026-11-10): the branch's commits its
 *  upstream lacks — or, on a branch with no upstream yet (never pushed) that
 *  is not the default branch, its commits ahead of the default branch. */
export function gitPushCount(r: RailGitRepo): number {
  if (r.upstream) return r.upstream.ahead;
  if (r.branch === null || r.branch === r.default_branch) return 0;
  return r.ahead ?? 0;
}

/** WHAT A PULL WOULD BRING: the upstream's commits this branch lacks, else
 *  the default branch's (the old green, "behind main"). */
export function gitPullCount(r: RailGitRepo): number {
  return Math.max(r.upstream?.behind ?? 0, r.behind ?? 0);
}

export type GitDoorDot = "attention" | "running" | "unread" | "push";

/** THE GIT DOOR'S TWO DOTS (owner, 2026-11-10): the top-right one red (the
 *  last check failed) > yellow (checking) > green (something to pull) > blue
 *  (something to push); a second, bottom-right, blue while there is something
 *  to push and the top one is not already saying so. */
export function gitDoorDots(s: { checking: boolean; checked: boolean | null; repo: RailGitRepo }): {
  top: GitDoorDot | null;
  second: "push" | null;
} {
  const push = gitPushCount(s.repo) > 0;
  const check = gitChipDot({ checking: s.checking, checked: s.checked, behind: gitPullCount(s.repo) });
  const top: GitDoorDot | null = check ?? (push ? "push" : null);
  return { top, second: push && top !== "push" ? "push" : null };
}

/** The push / pull counts in words, for the door's tooltip and label: what
 *  the upstream says ("2 to push", "1 to pull"), or an unpublished branch's
 *  "3 not pushed yet". The default branch's own counts are `gitCounts`'. */
export function gitSyncWords(r: RailGitRepo): string[] {
  const out: string[] = [];
  const push = gitPushCount(r);
  if (push > 0) out.push(r.upstream ? `${push} to push` : `${push} not pushed yet`);
  const pull = r.upstream?.behind ?? 0;
  if (pull > 0) out.push(`${pull} to pull`);
  return out;
}

/** The dots' words, for the door's `aria-label`. */
export const GIT_DOT_WORDS: Record<GitDoorDot, string> = {
  attention: "the last check did not reach the remote",
  running: "checking for updates",
  unread: "updates on the remote",
  push: "commits to push",
};

/** "N ahead · M behind main" / "No remote" / the unknown default branch. */
export function gitCounts(r: RailGitRepo): string {
  if (!r.has_remote) return "No remote";
  if (r.default_branch === null) return "Default branch not known yet — check to find out";
  return `${r.ahead ?? 0} ahead · ${r.behind ?? 0} behind ${r.default_branch}`;
}

export function gitBranch(r: RailGitRepo): string {
  return r.branch ?? "detached HEAD";
}

/** THE DOOR'S TOOLTIP (owner, 2026-11-10): `Git — on <branch> · <counts>`,
 *  then the push / pull words. */
export function gitDoorTitle(r: RailGitRepo): string {
  return [`Git — on ${gitBranch(r)}`, gitCounts(r), ...gitSyncWords(r)].join(" · ");
}
