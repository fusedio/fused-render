// THE GIT CHIP (owner, 2026-11-07, behind the rail flag: "remove git icons
// from the sidebar, and instead beside Terminal say Git and when clicked opens
// a popover with the git UI. Also the green dot, yellow dot and red dot we
// have for git should still pop up no matter what").
//
// What the rail's Git section drew in a 48px column of glyphs and hints, said
// in words in a status-bar panel: the branch and how far it stands from the
// remote's default branch, Check for updates, the last commits — each commit
// and "Open Git panel" the page's Git panel door where it has one. The facts
// are the page rail's (rail-git-store.ts): the chip stands only while the
// flag is on and a mounted rail has a repository.
//
// CLICK, NOT HOVER — TerminalDock's call, not useStatusChip's: this panel is
// something you act in (Check, a commit's door), and a hover preview opening
// beside the Terminal chip on every pass of the pointer would be noise. Still
// one panel at a time (`useExclusiveSection`) and the bar's outside/Escape
// dismissal (`useDismissOnOutside`); a second click closes it.
//
// SPLIT INTO A PURE VIEW (`GitDockView`) AND A STATEFUL WRAPPER, the
// TerminalDock/ModelsDock split: the view renders with fixed props in tests.
import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowDown, ArrowUp, GitBranch, Loader2, RefreshCw } from "lucide-react";
import StatusChip from "@platform/ui/StatusChip";
import { useExclusiveSection } from "@platform/lib/exclusiveSection";
import { useDismissOnOutside } from "@platform/lib/dismissOnOutside";
import { useCanvasRailPeek } from "./claude-rail/rail-peek-flag";
import { useRailGitDoor, type RailGitDoor } from "./claude-rail/rail-git-store";
import { gitBranch, gitChipDot, gitCounts } from "./git-dock-lib";

const DOT_WORDS = {
  attention: "the last check did not reach the remote",
  running: "checking for updates",
  unread: "updates on the remote",
} as const;

export function GitDockView({
  git,
  open,
  onToggle,
  onClose,
  hostRef,
}: {
  git: RailGitDoor;
  open: boolean;
  onToggle: () => void;
  /** Close the panel (after a door is taken). */
  onClose: () => void;
  hostRef?: React.RefObject<HTMLDivElement>;
}) {
  const r = git.repo;
  const branch = gitBranch(r);
  const counts = gitCounts(r);
  const dot = gitChipDot({ checking: git.checking, checked: git.checked, behind: r.behind });
  const door = git.onOpenGit;
  // Ahead / behind as ↑n ↓n tags beside the branch, only when non-zero
  // (owner, 2026-11-07: "show a number with an arrow, like other interfaces";
  // the "0 ahead · 0 behind main" line is gone). The words stay in the hint.
  const base = r.default_branch ?? "the default branch";
  const ahead = r.ahead ?? 0;
  const behind = r.behind ?? 0;
  const take = () => {
    onClose();
    door?.();
  };
  return (
    <div className="dl-host git-chip" ref={hostRef}>
      <StatusChip
        label="Git"
        tone={open ? "on" : "idle"}
        open={open}
        pinned={open}
        title={`On ${branch} · ${counts}`}
        ariaLabel={`Git, on ${branch}` + (dot ? `, ${DOT_WORDS[dot]}` : "")}
        // The slot only while there is a dot: an empty one would leave a gap
        // after "Git" on every page.
        {...(dot ? { dot } : {})}
        onClick={onToggle}
      />
      {open && (
        <div className="dl-panel git-panel" role="dialog" aria-label="Git">
          <div className="git-panel-head">
            {/* One row: the branch on the left, its two actions on the right —
                Check for updates as an icon (the Updates chip's), the Git
                panel's door as an outlined Open (owner, 2026-11-07). */}
            <div className="git-panel-top">
              <div className="git-panel-branch">
                <GitBranch size={13} aria-hidden="true" className="git-panel-branch-icon" />
                <span className="git-panel-branch-name" title={counts}>
                  {branch}
                </span>
                {ahead > 0 && (
                  <span className="git-panel-tag" data-hint={`${ahead} ahead of ${base}`} aria-label={`${ahead} ahead of ${base}`}>
                    <ArrowUp size={11} aria-hidden="true" />
                    {ahead}
                  </span>
                )}
                {behind > 0 && (
                  <span className="git-panel-tag" data-hint={`${behind} behind ${base}`} aria-label={`${behind} behind ${base}`}>
                    <ArrowDown size={11} aria-hidden="true" />
                    {behind}
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
                {door && (
                  <button type="button" className="git-panel-open" onClick={take}>
                    Open
                  </button>
                )}
              </div>
            </div>
            {git.checked === false && !git.checking && (
              <div className="git-panel-warn" role="status">
                No fresh answer from the remote yet
              </div>
            )}
          </div>
          {r.commits.length > 0 && (
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
                    {door ? (
                      <button type="button" className="git-commit" title={`${c.subject} — open the Git panel`} onClick={take}>
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
          )}
        </div>
      )}
    </div>
  );
}

export default function GitDock() {
  const peekOn = useCanvasRailPeek();
  const git = useRailGitDoor();
  const [open, setOpen] = useState(false);
  const hostRef = useRef<HTMLDivElement>(null);
  const shown = peekOn && git !== null;
  const isOpen = open && shown;
  const close = useCallback(() => setOpen(false), []);
  // The chip going (a path change clears the repo, a page without one, the
  // flag off) closes its panel: coming back to a repo must not reopen it on
  // its own and take the bar's one panel from whatever is open there (Bugbot).
  useEffect(() => {
    if (!shown) setOpen(false);
  }, [shown]);
  useExclusiveSection("git", isOpen, close);
  useDismissOnOutside(hostRef, isOpen, close);
  if (!shown) return null;
  return (
    <GitDockView git={git} open={isOpen} onToggle={() => setOpen((o) => !o)} onClose={close} hostRef={hostRef} />
  );
}
