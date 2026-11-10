// THE GIT CARD'S BODY (claude-rail GitPeek; owner, 2026-11-10: the Git door's
// card is "the bottom Git popup's content"): what the status bar's Git chip
// drew in its panel until the door replaced it — the branch and how far it
// stands from the remote's default branch and its own upstream, Check for
// updates, the warn line, and the commits — each commit the page's Git
// panel door where it has one (`onTake`). A pure view: it renders with fixed
// props in tests.
//
// THE COMMITS ARE THE SCROLLING SECTION (owner, 2026-11-10: "fill it with
// commits"): the branch row and the warn line stay put, the list takes the
// card's height and scrolls inside it (claude-rail.css `.git-panel-commits`).
import { ArrowDown, ArrowUp, GitBranch, Loader2, RefreshCw } from "lucide-react";
import type { RailGitDoor } from "./claude-rail/rail-git-store";
import { gitBranch, gitCounts, gitPullCount, gitPushCount } from "./git-dock-lib";

export function GitPanelBody({ git, onTake }: { git: RailGitDoor; onTake?: () => void }) {
  const r = git.repo;
  const branch = gitBranch(r);
  const counts = gitCounts(r);
  // Ahead / behind as ↑n ↓n tags beside the branch, only when non-zero
  // (owner, 2026-11-07: "show a number with an arrow, like other interfaces";
  // the "0 ahead · 0 behind main" line is gone). The words stay in the hint.
  const base = r.default_branch ?? "the default branch";
  const ahead = r.ahead ?? 0;
  const behind = r.behind ?? 0;
  // …and what a push / pull would move (owner, 2026-11-10): the upstream's,
  // or an unpublished branch's commits — the door's blue and green dots.
  const push = gitPushCount(r);
  // The green dot's count (CodeRabbit on #2236): the upstream's behind, or
  // the default branch's — a dot never stands without its "to pull".
  const pull = gitPullCount(r);
  const pushWords = r.upstream ? `${push} to push` : `${push} not pushed yet`;
  // ON THE DEFAULT BRANCH ITSELF the vs-default pair says what the upstream
  // pair says (origin/<default> is both): only the to push / to pull pair
  // (QA, 2026-11-10: "↑1 ↓1 ↑1 to push ↓1 to pull" was noise). Elsewhere
  // both, the vs-default ones saying so in their hint.
  const vsDefault = !(r.upstream && r.branch !== null && r.branch === r.default_branch);
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
            {/* …not when it says what "to pull" says (the same count). */}
            {vsDefault && behind > 0 && behind !== pull && (
              <span className="git-panel-tag" data-hint={`${behind} behind, vs ${base}`} aria-label={`${behind} behind ${base}`}>
                <ArrowDown size={11} aria-hidden="true" />
                {behind}
              </span>
            )}
            {push > 0 && (
              <span className="git-panel-tag is-push" data-hint={pushWords} aria-label={pushWords}>
                <ArrowUp size={11} aria-hidden="true" />
                {push} to push
              </span>
            )}
            {pull > 0 && (
              <span className="git-panel-tag is-pull" data-hint={`${pull} to pull`} aria-label={`${pull} to pull`}>
                <ArrowDown size={11} aria-hidden="true" />
                {pull} to pull
              </span>
            )}
          </div>
          <div className="git-panel-tools">
            <button
              type="button"
              className="upd-check-btn git-panel-check"
              disabled={!r.has_remote || git.checking}
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
          </div>
        </div>
        {git.checked === false && !git.checking && (
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
