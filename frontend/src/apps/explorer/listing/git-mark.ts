// How a git status becomes a decoration on a listing row: the class the name is
// tinted through, and the one-letter badge beside it.
//
// The BADGE is not decoration on the decoration. Colour is the whole signal in
// VS Code's file tree, and colour alone excludes anyone who cannot separate the
// green from the amber — roughly one reader in twelve for the commonest form of
// colour-vision deficiency, which is exactly the added/modified pair. The
// letter says the same thing without the hue, and its tooltip says it in words
// for a screen reader.
//
// Letters follow git's own porcelain vocabulary rather than our state names, so
// they mean the same thing here as in `git status` — `U` for untracked (`??`),
// `M` for a dirty work tree, `S` for staged-and-clean, `!` for an unresolved
// merge. `A` is deliberately unused: git spells the STAGED add `A`, and lending
// that letter to "untracked" would make the two states swap names between the
// listing and the terminal.
import type { GitEntryStatus } from "@platform/lib/api";

export interface GitMark {
  letter: string;
  label: string;
}

export const GIT_MARKS: Record<GitEntryStatus, GitMark> = {
  conflicted: { letter: "!", label: "Unresolved merge conflict" },
  modified: { letter: "M", label: "Modified, not staged" },
  untracked: { letter: "U", label: "Untracked — new to git" },
  staged: { letter: "S", label: "Staged for the next commit" },
};

/**
 * The row's git class, or `""` when there is nothing to tint it with.
 *
 * Unknown values answer `""` for the same reason `gitMarkFor` answers null:
 * the field crosses a version boundary (a newer server talking to a shell
 * built before a fifth state existed), and an unstyled `git-…` class is worse
 * than an undecorated row. The two functions therefore agree on every input —
 * a row is never tinted without its badge, or badged without its tint.
 */
export function gitRowClass(status: string | undefined): string {
  return gitMarkFor(status) ? ` git-${status}` : "";
}

/** The mark for an entry, or null when git has nothing to say about it. */
export function gitMarkFor(status: string | undefined): GitMark | null {
  if (!status) return null;
  return GIT_MARKS[status as GitEntryStatus] ?? null;
}
