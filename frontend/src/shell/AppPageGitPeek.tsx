// THE APP PAGE'S GIT PEEK — the folder's `git` template beside the whole page,
// framed the way the TASKS TAB'S OWN side peek is (styles/task-peek.css,
// TaskPeek.tsx's `.task-side-peek`), not the way the explorer's file preview
// borrows a companion column (apps/explorer/PreviewSidebar).
//
// THE OWNER'S OWN WORDS: "I just want the git template. not the full right
// sidebar. I want the UI to be similar more like the tasks tab sidebar." The
// draft that shipped first used PreviewSidebar — a mode rail with a "Git" tab
// header, a panel-toggle icon, a close button — because it was the nearest
// component that already framed a borrowed template beside a page. All of
// that chrome is gone here: this is the FULL git template (staging,
// committing, branches, push/pull — nothing trimmed) with none of the
// surrounding sidebar shell around it, in a slim peek of its own: a close
// affordance, a resize seam on its left edge, a body.
//
// REUSES THE TASK PEEK'S CSS VARIABLES, not its component or its store.
// `--peek-dur`/`--peek-ease` (200ms ease, styles/task-peek.css) are declared
// on `:root` — global, not scoped to `.task-side-peek` — precisely so a second
// peek elsewhere in the app can open and close on the same rhythm without
// duplicating the numbers. `task-peek-store.ts` (the open task, its dragged
// width, the `?peek=` URL) is Tasks-specific state and is not imported here at
// all; this panel's own open/failed state lives in useAppPageGitColumn.ts, and
// its own width in useAppPageGitPeekWidth.ts (see that file for why the width
// is local rather than routed through the explorer's shared side-store).
//
// WHERE THIS SITS RELATIVE TO THE TASKS TAB'S OWN PEEK (decided, not
// discovered — see DECISIONS.md): OUTSIDE it, one level up. AppPage.tsx's
// `.app-page-split` is the row both live in — this panel is one of its two
// children, the other being the frame that holds `TaskPeekFrame` (which is
// itself a row-inside-a-row on the Tasks tab). The two peeks can never share
// a right edge: the Tasks peek's `.tasks-peek-host` is scoped to the width
// this component's frame sibling leaves it, so opening this panel narrows the
// Tasks tab's own peek host exactly as it narrows every other tab's content,
// and the Tasks peek renders inside THAT already-narrowed row, never
// underneath or beside this one.
import { type ReactElement, useEffect, useRef } from "react";
import { X } from "lucide-react";
import type { DirMode } from "@apps/explorer/lib/dir-mode";
import type { AppPageGitPeekWidth } from "./useAppPageGitPeekWidth";

export interface AppPageGitPeekProps {
  open: boolean;
  mode: DirMode;
  src: string | null;
  onClose: () => void;
  layout: AppPageGitPeekWidth;
}

export default function AppPageGitPeek({
  open,
  mode,
  src,
  onClose,
  layout,
}: AppPageGitPeekProps): ReactElement {
  const { width, onSeamPointerDown } = layout;

  // `closeGit` (useAppPageGitColumn.ts) drops `src` to null a commit BEFORE
  // this panel finishes its 200ms slide-out (`useDirMode`'s reset to ABSENT
  // runs one commit after `open` flips false) — without this, the reader
  // watches the live template blink to "Loading…" and only then slide away.
  // Kept only while shut: reopening the SAME panel while `src` is still
  // resolving must show "Loading…", not a stale frame from before it closed,
  // so this is read only when `!open`.
  const lastSrc = useRef<string | null>(null);
  useEffect(() => {
    if (src !== null) lastSrc.current = src;
  }, [src]);
  const shownSrc = open ? src : (src ?? lastSrc.current);

  return (
    <aside
      className={"app-git-peek" + (open ? " is-open" : "")}
      style={{ width: `${width}px` }}
      aria-hidden={!open}
    >
      <div
        className="app-git-peek-seam"
        onPointerDown={onSeamPointerDown}
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize the git panel"
      />
      <div className="app-git-peek-head">
        <span className="app-git-peek-title">Git</span>
        <button
          type="button"
          className="app-git-peek-close"
          title="Close"
          aria-label="Close the git panel"
          onClick={onClose}
        >
          <X />
        </button>
      </div>
      <div className="app-git-peek-body">
        {mode.pending || shownSrc === null ? (
          mode.failed ? (
            // A REJECTED probe, not a settled "no git here" (dir-mode.ts) —
            // the panel stays open and says so, rather than silently closing
            // over an error the reader never saw. Closing and reopening
            // (App Doctor's "Open in git" row) retries for real: the failed
            // fetch's cache entry is evicted on the spot (dir-mode.ts).
            <p className="app-git-peek-error">
              Could not check this folder for git. Close this panel and open
              it again to retry.
            </p>
          ) : (
            <p className="app-git-peek-resolving">Loading…</p>
          )
        ) : (
          <iframe
            key={shownSrc}
            className="app-git-peek-frame"
            src={shownSrc}
            title="Git"
          />
        )}
      </div>
    </aside>
  );
}
