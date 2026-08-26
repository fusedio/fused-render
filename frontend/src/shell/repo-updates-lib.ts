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
  ahead: number;
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
 * The working-tree clause of the prompt below, or "" when the refusal
 * `reason` doesn't actually tell us the tree's state. Only ever asserts
 * what the server's own refusal reason establishes:
 *
 *  - "dirty"       — the preflight's own `git status` found uncommitted
 *                    changes; this is the one reason that means "clean"
 *                    would be false.
 *  - "in-progress" — a rebase (or other operation) was already under way
 *                    BEFORE this refusal, so the tree is unmerged, not
 *                    merely "not clean" in the ordinary sense.
 *  - "git-failed"  — the mutation's own git command failed AFTER the
 *                    preflight passed clean — most commonly a rebase
 *                    conflict, which leaves the tree conflicted even
 *                    though it was clean a moment before. Never claim
 *                    "clean" here; the true state needs the git panel.
 *  - anything else (missing/mount/detached/no-remote/unknown-repo) — the
 *    mutation never got far enough to say anything about the tree at all.
 */
function workingTreeClause(reason?: string): string {
  if (reason === "dirty") return ", working tree dirty (uncommitted changes)";
  if (reason === "in-progress") return ", working tree mid-operation (a rebase was already in progress)";
  if (reason === "git-failed") {
    return ", working tree state unknown after the failure — check the git panel " +
      "for a conflict (a rebase conflict is the most common cause)";
  }
  return "";
}

/**
 * The "Fix with Claude" prompt for a refused update/rebase — the same
 * material `templates/git/template.html`'s `askClaudeOnError` assembles for
 * the git companion's own button (template.html:2580-2605): the error, the
 * branch, ahead/behind, the working-tree state (only ever what the refusal
 * reason actually tells us — see `workingTreeClause`), and the repo root as
 * the working directory to fix it in. Built here rather than in the
 * component so it is testable without a DOM, same as everything else in
 * this file.
 */
export function repoFixPrompt(row: RepoRow, message: string, reason?: string): string {
  const repo = row.repo;
  const parts = [`A git operation failed in a GUI. The error was:\n${message}`];
  const branch = repo.branch || "(detached)";
  parts.push(
    `Repository state: branch ${branch}, tracking origin/${repo.default_branch}, ` +
      `${repo.ahead} ahead / ${repo.behind} behind${workingTreeClause(reason)}.`
  );
  parts.push(`This repository/working directory is ${repo.root}.`);
  parts.push(
    "Explain what the error means, then fix it: run whatever is needed to " +
      "get this repository out of the failure and back to a good state."
  );
  return parts.join("\n\n");
}
