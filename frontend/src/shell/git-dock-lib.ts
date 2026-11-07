// The Git chip's words and dot (shell/GitDock.tsx), pure — what the rail's
// Git section said in its hints (ClaudeRail, the section the rail flag moves
// to the status bar), said once here so the two cannot drift.
import type { RailGitRepo } from "@platform/lib/api";

/** THE DOT, always on while it has something to say (owner, 2026-11-07: "the
 *  green dot, yellow dot and red dot we have for git should still pop up no
 *  matter what"). Red > yellow > green:
 *    attention (red)   the last check did not reach the remote;
 *    running (yellow)  a check is out;
 *    unread (green)    the remote's default branch has commits this one lacks.
 *  A re-check after a failed one stays red until it answers (the popover's
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

/** "N ahead · M behind main" / "No remote" / the unknown default branch. */
export function gitCounts(r: RailGitRepo): string {
  if (!r.has_remote) return "No remote";
  if (r.default_branch === null) return "Default branch not known yet — check to find out";
  return `${r.ahead ?? 0} ahead · ${r.behind ?? 0} behind ${r.default_branch}`;
}

export function gitBranch(r: RailGitRepo): string {
  return r.branch ?? "detached HEAD";
}
