// The rules for repo-update rows in their own sibling notification card
// (SPEC §36, D555 — no longer rows inside the jobs/downloads activity card),
// kept pure so they can be tested without a DOM or a poll — the same split
// queue-dock-lib.ts makes against ActivityDock.tsx: what a row SAYS and WHICH
// action it offers live here; the polling, the card's own plate/header/fold
// and the pixels all live in RepoUpdatesDock.tsx.
//
// The rows this module builds never decide "is this repo worth mentioning" —
// that answer is the server's (fused_render/git_upstream.py's `known_repos`,
// behind GET /api/git-upstream): it already reports only repos with a
// non-zero behind count. What this file decides is presentation only: the
// row's label, and — the one real decision here — which action is PRIMARY.
import { urlForFsPath } from "@platform/lib/router";

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

/** R8 — the explorer deep link a repo row's clickable body opens: the repo's
 *  own folder, with `_side=git` so the listing's companion pane lands open on
 *  its Git tab rather than whatever it was last left on (or closed) — an
 *  explicit `_side` on the url wins over that session state, and an absent
 *  one would mean "whatever the pane already shows", which is not what a
 *  click on "Newer changes available" promises. The open/close semantics of
 *  that param live in apps/explorer/listing/pane-side.ts; this just spells
 *  the one value this row ever asks for. */
export function repoGitHref(root: string): string {
  return urlForFsPath(root, "?_side=git");
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
export function repoFixPrompt(
  row: RepoRow,
  message: string,
  reason?: string,
  details: FixDetails = {},
): string {
  const repo = row.repo;
  const branch = repo.branch || "(detached)";
  return buildFixPrompt({
    ...details,
    message,
    state:
      `Repository state: branch ${branch}, tracking origin/${repo.default_branch}, ` +
      `${repo.ahead} ahead / ${repo.behind} behind${workingTreeClause(reason)}.`,
    root: repo.root,
  });
}

/** What a failed git step can tell Claude beyond its one-line message. */
export interface FixDetails {
  /** The action taken, e.g. "Clicked Update", "Auto-push after Claude commit". */
  action?: string;
  /** The actual git command that failed. */
  command?: string;
  /** git's COMPLETE output (never truncated). */
  output?: string;
}

/**
 * The confirm-first protocol every Fix with Claude entry point carries
 * (git view toast, notification dock, auto-sync failure rows). The same text
 * is mirrored in templates/git/template.html (a template cannot import this
 * module), and tests on both sides pin the key sentences. Nothing here
 * changes anything itself: it TELLS Claude to overview, wait for approval,
 * apply, then ask about pushing.
 */
export const FIX_PROTOCOL =
  "Work in this order, and do not skip a step:\n" +
  "1. OVERVIEW FIRST. Change nothing yet (read-only git commands are fine). " +
  "Tell me the strategy (merge vs rebase), a one-line verdict per file " +
  "(keep mine / keep theirs / combine / new from remote / new locally; file " +
  "names only, no diffs), and whether you intend to push.\n" +
  "2. CONFIRM. Ask me to approve that plan (use the plan-approval or " +
  "question tool) and wait. Make no change to the repository before I approve.\n" +
  "3. APPLY the plan once I approve.\n" +
  "4. Then ask me whether to push. Never push without my explicit yes.";

/** The shared body: action, command, complete output, state, root, protocol. */
export function buildFixPrompt(p: FixDetails & { message: string; state: string; root: string }): string {
  const parts = ["A git operation failed in a GUI."];
  if (p.action) parts.push(`Action taken: ${p.action}`);
  if (p.command) parts.push(`Git command: ${p.command}`);
  const full = (p.output || "").trim();
  parts.push(`Git's complete output:\n${full || p.message}`);
  if (full && p.message && !full.includes(p.message)) parts.push(`The app summarised it as: ${p.message}`);
  parts.push(p.state);
  parts.push(`This repository/working directory is ${p.root}.`);
  parts.push(FIX_PROTOCOL);
  return parts.join("\n\n");
}

/** A standing auto-sync failure as served by GET /api/git-upstream. */
export interface SyncFailure {
  id: string;
  root: string;
  name: string;
  reason: string;
  title: string;
  action: string;
  command: string;
  output: string;
  push: boolean;
  at: number;
}

/** A recent auto-pull that brought commits in (the transient popup). */
export interface SyncPull {
  id: string;
  root: string;
  name: string;
  count: number;
  at: number;
}

/** The Fix with Claude prompt for an auto-sync failure row. */
export function syncFixPrompt(f: SyncFailure): string {
  return buildFixPrompt({
    action: f.action,
    command: f.command,
    output: f.output,
    message: f.title,
    state: `Failure: ${f.title}.`,
    root: f.root,
  });
}

/** "Updated <app> with N changes" — singular for one. */
export function pullPopupTitle(p: SyncPull): string {
  return `Updated ${p.name} with ${p.count} ${p.count === 1 ? "change" : "changes"}`;
}

/** Pulls not yet announced, given the ids already seen. */
export function newPulls(pulls: SyncPull[], seen: ReadonlySet<string>): SyncPull[] {
  return pulls.filter((p) => !seen.has(p.id));
}

/** The buttons a failure row offers, in display order (first = the head
 *  slot beside the dismiss, second = the line below the status). Keyed on the
 *  server's failure `reason` vocabulary (git_upstream._FAILURE_TITLES); Retry
 *  is offered only where re-running the sync can change the outcome: a dirty
 *  tree or a diverged branch stays that way until the user acts. "sign-in"
 *  lands on the git view, the only place the sign-in flow lives. */
export type SyncFailureActionId = "open-git" | "sign-in" | "retry" | "fix";

export interface SyncFailureAction {
  id: SyncFailureActionId;
  label: string;
}

const ACTION_LABELS: Record<SyncFailureActionId, string> = {
  "open-git": "Open git view",
  "sign-in": "Sign in",
  retry: "Retry",
  fix: "Fix with Claude",
};

export function syncFailureActions(reason: string): SyncFailureAction[] {
  const ids: SyncFailureActionId[] =
    reason === "dirty"
      ? ["open-git", "fix"]
      : reason === "diverged"
        ? ["fix"]
        : reason === "auth"
          ? ["sign-in", "retry"]
          : ["retry", "fix"];
  return ids.map((id) => ({ id, label: ACTION_LABELS[id] }));
}
