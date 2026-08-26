// The rules for repo-update rows in the one bottom-right activity card, kept
// pure so they can be tested without a DOM or a poll — the same split
// queue-dock-lib.ts makes against QueueDock.tsx: what a row SAYS and WHICH
// action it offers live here; the polling and the pixels live in
// RepoUpdatesDock.tsx, and the card around it is DownloadManager's.
//
// The rows this module builds never decide "is this repo worth mentioning" —
// that answer is the server's (fused_render/git_upstream.py's `known_repos`,
// behind GET /api/git-upstream): it already reports only repos with a
// non-zero behind count. What this file decides is presentation only: the
// row's label, and — the one real decision here — which action is PRIMARY.

export interface RepoStatus {
  root: string;
  branch: string | null;
  default_branch: string;
  on_default: boolean;
  behind: number;
  checked_at: number;
}

export type RepoAction = "update" | "rebase";

export interface RepoRow {
  repo: RepoStatus;
  /** The repo's own last path segment — a row has no room for the full
   *  path; that lives in the title tooltip instead. */
  name: string;
  /** "update" — the ROW's primary action, on the default branch: an
   *  --ff-only pull, which can never conflict.
   *  "rebase" — everywhere else: the row is informational (it names how far
   *  behind the default branch is) with Rebase as a SECONDARY action, since
   *  rebasing rewrites the current branch's commits and can conflict. */
  primaryAction: RepoAction;
}

/** The repo's own last path segment, forward-slash or backslash either way. */
export function repoName(root: string): string {
  const norm = (root || "").replace(/\\/g, "/").replace(/\/+$/, "");
  const i = norm.lastIndexOf("/");
  return i === -1 ? norm : norm.slice(i + 1);
}

/**
 * One row per repo the server reported, primary action decided purely by
 * branch shape — `on_default` — never by the behind count. A repo on a
 * feature branch that is only one commit behind is exactly as much "not the
 * default branch" as one that is fifty behind; the count changes the
 * SENTENCE, not the ACTION.
 */
export function repoRows(repos: RepoStatus[] | undefined): RepoRow[] {
  return (repos || []).map((repo) => ({
    repo,
    name: repoName(repo.root),
    primaryAction: repo.on_default ? "update" : "rebase",
  }));
}

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

/** The row's one line of status text — what `origin/<default>` has that this
 * branch does not, phrased against whichever branch is actually checked out. */
export function repoStatusText(row: RepoRow): string {
  const commits = plural(row.repo.behind, "commit");
  if (row.primaryAction === "update") {
    return `origin/${row.repo.default_branch} is ${commits} ahead`;
  }
  const branch = row.repo.branch || "this branch";
  return `origin/${row.repo.default_branch} is ${commits} ahead of ${branch}`;
}

/** The primary button's label. */
export function repoActionLabel(action: RepoAction): string {
  return action === "update" ? "Update" : "Rebase";
}

/**
 * The "Fix with Claude" prompt for a refused update/rebase — the same
 * material `templates/git/template.html`'s `askClaudeOnError` assembles for
 * the git companion's own button (template.html:2580-2605): the error, the
 * branch, ahead/behind, the dirty flag, and the repo root as the working
 * directory to fix it in. Built here rather than in the component so it is
 * testable without a DOM, same as everything else in this file.
 */
export function repoFixPrompt(row: RepoRow, message: string, reason?: string): string {
  const repo = row.repo;
  const parts = [`A git operation failed in a GUI. The error was:\n${message}`];
  const branch = repo.branch || "(detached)";
  const dirty = reason === "dirty" ? "dirty" : "clean";
  parts.push(
    `Repository state: branch ${branch}, tracking origin/${repo.default_branch}, ` +
      `0 ahead / ${repo.behind} behind, working tree ${dirty}.`
  );
  parts.push(`This repository/working directory is ${repo.root}.`);
  parts.push(
    "Explain what the error means, then fix it: run whatever is needed to " +
      "get this repository out of the failure and back to a good state."
  );
  return parts.join("\n\n");
}
