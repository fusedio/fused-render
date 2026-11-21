// THE GIT CARD'S BODY (claude-rail GitPeek; owner, 2026-11-10: the Git door's
// card is "the bottom Git popup's content"): what the status bar's Git chip
// drew in its panel until the door replaced it — the branch and how far it
// stands from the remote's default branch, Check for updates, the Git view's
// sync button, the warn line, and the commits — each commit the page's Git
// panel door where it has one (`onTake`). A pure view: it renders with fixed
// props in tests.
//
// THE SYNC BUTTON IS THE PANE'S (owner, 2026-11-21: the "↑2 to push" pill
// read as a button and did nothing — "follow the fetch and push button we
// have in panel"): templates/git/template.html's `remote` group, same rules,
// same order — one primary at a time (Get latest, else Send, else Publish
// branch) wearing its count, the quiet fetch icon beside it. It runs the
// pane's own op (`sync`, POST /api/git/rail/sync → ops.py).
//
// THE COMMITS ARE THE SCROLLING SECTION (owner, 2026-11-10: "fill it with
// commits"): the branch row and the warn line stay put, the list takes the
// card's height and scrolls inside it (claude-rail.css `.git-panel-commits`).
import { ArrowDown, ArrowDownToLine, ArrowUp, ArrowUpToLine, GitBranch, Loader2, RefreshCw } from "lucide-react";
import type { RailGitRepo, RailGitSyncOp } from "@platform/lib/api";
import type { RailGitDoor } from "./claude-rail/rail-git-store";
import { gitBranch, gitCounts } from "./git-dock-lib";

interface SyncAct {
  op: RailGitSyncOp;
  label: string;
  count: number | null;
  hint: string;
  aria: string;
}

const plural = (n: number, one: string) => `${n} ${one}${n === 1 ? "" : "s"}`;

/** The pane's morphing button for `r`, or null (then the fetch icon stands
 *  alone): what the upstream says a pull / push would move. */
export function gitSyncAct(r: RailGitRepo): SyncAct | null {
  // A copy of someone else's app: the pane routes its push / pull (Bring in,
  // the app page) — the card offers neither rather than a dead-end refusal.
  if (!r.has_remote || r.branch === null || r.guarded) return null;
  const behind = r.upstream?.behind ?? 0;
  const ahead = r.upstream?.ahead ?? 0;
  if (behind > 0) {
    return {
      op: "pull",
      label: "Get latest",
      count: behind,
      hint:
        (ahead > 0 ? `You also have ${plural(ahead, "commit")} to send — get the latest first. ` : "") +
        "git pull --ff-only",
      aria: `Get the latest — ${plural(behind, "commit")} to pull`,
    };
  }
  if (ahead > 0) {
    return { op: "push", label: "Send", count: ahead, hint: "git push", aria: `Send ${plural(ahead, "commit")}` };
  }
  if (!r.upstream && r.commits.length > 0) {
    return {
      op: "push",
      label: "Publish branch",
      count: null,
      hint: `git push — create ${r.branch} on the remote and track it`,
      aria: `Publish ${r.branch}`,
    };
  }
  return null;
}

export function GitPanelBody({ git, onTake }: { git: RailGitDoor; onTake?: () => void }) {
  const r = git.repo;
  const branch = gitBranch(r);
  const counts = gitCounts(r);
  // Ahead / behind as ↑n ↓n tags beside the branch, only when non-zero
  // (owner, 2026-11-07: "show a number with an arrow, like other interfaces";
  // the "0 ahead · 0 behind main" line is gone). The words stay in the hint.
  // Information, not actions — what a push / pull would move is the button.
  const base = r.default_branch ?? "the default branch";
  const ahead = r.ahead ?? 0;
  const behind = r.behind ?? 0;
  // ON THE DEFAULT BRANCH ITSELF the vs-default pair says what the button
  // says (origin/<default> is both its upstream and the base): not drawn.
  const vsDefault = !(r.upstream && r.branch !== null && r.branch === r.default_branch);
  const act = git.sync ? gitSyncAct(r) : null;
  const syncing = git.syncing ?? null;
  const busy = git.checking || syncing !== null;
  const checkBtn = (
    <button
      type="button"
      className="upd-check-btn git-panel-check"
      disabled={!r.has_remote || busy}
      aria-busy={git.checking || undefined}
      aria-label={git.checking ? "Checking for updates…" : "Check for updates"}
      data-hint={!r.has_remote ? "No remote to check" : git.checking ? "Checking for updates…" : "Check for updates"}
      onClick={() => git.check()}
    >
      {git.checking ? (
        <Loader2 size={13} className="update-card-spin" aria-hidden="true" />
      ) : (
        <RefreshCw size={13} aria-hidden="true" />
      )}
    </button>
  );
  return (
    <div className="git-panel">
      <div className="git-panel-head">
        <div className="git-panel-top">
          <div className="git-panel-branch">
            <GitBranch size={13} aria-hidden="true" className="git-panel-branch-icon" />
            <span className="git-panel-branch-name" title={counts}>
              {branch}
            </span>
            {vsDefault && ahead > 0 && (
              <span className="git-panel-tag" data-hint={`${ahead} ahead, vs ${base}`} aria-label={`${ahead} ahead of ${base}`}>
                <ArrowUp size={11} aria-hidden="true" />
                {ahead}
              </span>
            )}
            {vsDefault && behind > 0 && (
              <span className="git-panel-tag" data-hint={`${behind} behind, vs ${base}`} aria-label={`${behind} behind ${base}`}>
                <ArrowDown size={11} aria-hidden="true" />
                {behind}
              </span>
            )}
          </div>
          <div className="git-panel-tools">
            {checkBtn}
            {act && (
              <button
                type="button"
                className="git-panel-sync"
                // aria-disabled, not disabled: the pressed button keeps focus
                // while its op runs.
                aria-disabled={busy || undefined}
                aria-busy={syncing === act.op || undefined}
                aria-label={act.aria}
                data-hint={act.hint}
                onClick={() => {
                  if (!busy) git.sync?.(act.op);
                }}
              >
                {syncing === act.op ? (
                  <Loader2 size={13} className="update-card-spin" aria-hidden="true" />
                ) : act.op === "pull" ? (
                  <ArrowDownToLine size={13} aria-hidden="true" />
                ) : (
                  <ArrowUpToLine size={13} aria-hidden="true" />
                )}
                <span>{act.label}</span>
                {act.count !== null && <span className="git-panel-sync-count">{act.count}</span>}
              </button>
            )}
          </div>
        </div>
        {git.syncNote && !syncing && <div className="git-panel-warn">{git.syncNote}</div>}
        {/* One live region for the sync, so success is said too. */}
        <span className="sr-only" role="status">
          {syncing === "push"
            ? "Sending…"
            : syncing === "pull"
              ? "Getting the latest…"
              : (git.syncNote ?? git.syncDone ?? "")}
        </span>
        {git.checked === false && !git.checking && !git.syncNote && (
          <div className="git-panel-warn" role="status">
            No fresh answer from the remote yet
          </div>
        )}
      </div>
      {r.commits.length > 0 ? (
        // One line a commit: its subject, its short sha on the right.
        <ul className="git-panel-commits" aria-label="Recent commits">
          {r.commits.map((c) => {
            const body = (
              <>
                <span className="git-commit-subject">{c.subject}</span>
                <span className="git-commit-sha">{c.short}</span>
              </>
            );
            return (
              <li key={c.sha}>
                {onTake ? (
                  <button type="button" className="git-commit" title={`${c.subject} — open the Git panel`} onClick={onTake}>
                    {body}
                  </button>
                ) : (
                  <div className="git-commit" title={c.subject}>
                    {body}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="git-panel-empty">No commits here yet</p>
      )}
    </div>
  );
}
