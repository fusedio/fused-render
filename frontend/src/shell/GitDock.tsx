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
import { useCallback, useRef, useState } from "react";
import StatusChip from "@platform/ui/StatusChip";
import { useExclusiveSection } from "@platform/lib/exclusiveSection";
import { useDismissOnOutside } from "@platform/lib/dismissOnOutside";
import { timeAgo } from "@platform/lib/format";
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
            <div className="git-panel-branch">
              On <strong>{branch}</strong>
            </div>
            <div className="git-panel-counts">{counts}</div>
            {git.checked === false && !git.checking && (
              <div className="git-panel-warn" role="status">
                No fresh answer from the remote yet
              </div>
            )}
          </div>
          <div className="git-panel-actions">
            <button
              type="button"
              className="update-card-btn"
              disabled={!r.has_remote || git.checking}
              aria-busy={git.checking || undefined}
              onClick={() => git.check()}
            >
              {git.checking ? "Checking…" : "Check for updates"}
            </button>
            {door && (
              <button type="button" className="update-card-ghost" onClick={take}>
                Open Git panel
              </button>
            )}
          </div>
          {r.commits.length > 0 && (
            <div className="dl-section">
              <div className="dl-section-head">
                Recent commits <span className="dl-section-count">{r.commits.length}</span>
              </div>
              <ul className="git-panel-commits">
                {r.commits.map((c) => {
                  const when = timeAgo(c.when) ?? "";
                  const who = [c.author, when].filter(Boolean).join(" · ");
                  const body = (
                    <>
                      <span className="git-commit-subject">{c.subject}</span>
                      <span className="git-commit-meta">
                        <span className="git-commit-who">{who}</span>
                        <span className="git-commit-sha">{c.short}</span>
                      </span>
                    </>
                  );
                  return (
                    <li key={c.sha}>
                      {door ? (
                        <button
                          type="button"
                          className="git-commit"
                          title={`${c.subject} — open the Git panel`}
                          onClick={take}
                        >
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
            </div>
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
  useExclusiveSection("git", isOpen, close);
  useDismissOnOutside(hostRef, isOpen, close);
  if (!shown) return null;
  return (
    <GitDockView git={git} open={isOpen} onToggle={() => setOpen((o) => !o)} onClose={close} hostRef={hostRef} />
  );
}
