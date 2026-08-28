// The rules for repo-update rows in their own sibling notification card
// (SPEC §36, D555 — no longer rows inside the jobs/downloads activity card),
// kept pure so they can be tested without a DOM or a poll — the same split
// queue-dock-lib.ts makes against QueueDock.tsx: what a row SAYS and WHICH
// action it offers live here; the polling, the card's own plate/header/fold
// and the pixels all live in RepoUpdatesDock.tsx.
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

// "rebase" was a member of this union, offered as a secondary action beside
// Switch (replaying the current branch onto the default). Removed — user
// call, D555 amendment: "the rebase button is scary, let's just remove it".
// Every row now offers exactly one action, so there is no secondary slot
// left to carry it.
export type RepoAction = "update" | "switch";

export interface RepoRow {
  repo: RepoStatus;
  /** The repo's own last path segment — a row has no room for the full
   *  path; that lives in the title tooltip instead. */
  name: string;
  /** "update" — the ROW's ONLY action, on the default branch: an
   *  --ff-only pull, which can never conflict.
   *  "switch" — everywhere else: a plain, non-destructive checkout of the
   *  default branch, offered off the default branch because it can never
   *  conflict or touch the user's own commits — the whole point of this
   *  card is to never override a user's work. */
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
    primaryAction: repo.on_default ? "update" : "switch",
  }));
}

/** The row's one line of status text. Deliberately generic — no remote name,
 * no branch name, no commit count: "origin/main is 1 commit ahead" reads as
 * a git status line to anyone who isn't fluent in git, and the row already
 * has a button that says what to do about it. The technical detail this
 * drops still reaches Claude in full via `repoFixPrompt` below, which is
 * written for a git-literate reader, not this one. */
export function repoStatusText(_row: RepoRow): string {
  return "Newer changes available";
}

/** A row's button label. `defaultBranch` is only consulted for "switch" —
 * the repo's ACTUAL default branch name (never a literal "main"), so pass
 * `row.repo.default_branch` at every call site that can offer switch. */
export function repoActionLabel(action: RepoAction, defaultBranch?: string): string {
  if (action === "update") return "Update";
  return `Switch to ${defaultBranch || "default"}`;
}

/**
 * What a dismissal is ABOUT — the repo's position, not the clock (D584 review
 * finding 3). `dismissed` maps repo root -> this signature, and a row stays
 * hidden for as long as its signature is unchanged.
 *
 * THIS USED TO BE `checked_at`, AND THAT WAS A REAL BUG. A dismissal expired
 * as soon as the server re-checked the repo, but `check_repo` stamps a fresh
 * `checked_at` on EVERY throttled re-check (`CHECK_TTL_S = 300`) whether or
 * not anything moved. So a dismissed row came back every five minutes — and
 * because leaving `visible` also drops it from `trackSeenIds`' seen set, the
 * return read as a genuine arrival and (since D574) POPPED THE PANEL OPEN over
 * whatever the user was doing. For anyone on a long-lived feature branch,
 * permanently behind, dismissal was durably useless.
 *
 * `behind` and `branch` are what the row is actually claiming — "this branch
 * is N commits behind" — so the dismissal expires exactly when that claim
 * changes: new upstream commits arrive, or the user checks out something else.
 * `RepoStatus` carries no HEAD sha, so `behind` is the closest honest proxy for
 * "upstream moved", and it needs no server change to be correct.
 */
export function repoDismissSignature(repo: RepoStatus): string {
  return `${repo.branch ?? ""}@${repo.behind}`;
}

/** Which rows a dismissal (decision C) still hides. No server state is needed:
 *  the row's own fields carry everything a client-side dismissal needs to
 *  expire itself — see `repoDismissSignature` for why they, and not
 *  `checked_at`, are the right fields. */
export function visibleRepoRows(
  rows: RepoRow[],
  dismissed: Record<string, string>
): RepoRow[] {
  return rows.filter((row) => dismissed[row.repo.root] !== repoDismissSignature(row.repo));
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
 * The "Fix with Claude" prompt for a refused update/switch — the same
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
