// The app page's right-hand GIT COLUMN (shell/AppPage.tsx): whether it is
// open, the borrowed `git` probe against the app FOLDER, and the template's
// `src`. Extracted out of AppPage.tsx's own body for the same reason
// useAppPageSnapshot.ts was (see that file's own header, and
// AppPage.test.tsx's): AppPage.tsx itself has no render-test precedent —
// mounting it pulls in base-ui's Tabs, a document-dependent keyboard-nav
// effect, useFavicon and the Tasks page's whole subtree — so a piece of it
// with real behaviour to pin gets its own hook, driven through the REAL
// `useDirMode` fetches (stubbed only at the network boundary) rather than
// hand-assigned state.
//
// ONE WAY IN, by the owner's choice: App Doctor's "Open in git" row
// (AppDoctorModal.tsx's `onOpenGit`), which until now left this page for the
// explorer. No header button, so a page nobody opens this from costs nothing
// at all — not a probe, not a frame: `open` gates the `useDirMode` call
// itself (`dir` becomes `null` while shut), the same "ask nothing until
// asked" rule `useDirMode`'s own header describes for its callers.
//
// The way OUT is `closeGit` — the column's own close button, and dragging it
// through its floor (PreviewSidebar's `onClose`) — OR the auto-close below:
// settled, and this folder does not offer git at all (no repository, or the
// gate said no). Ordinarily unreachable, since the Doctor row that calls
// `openGit` renders only once its own check has resolved a real repo root —
// this is the honest answer to a repository that went away between the
// report and the press, not a state anybody is meant to see.
import { useEffect, useRef, useState } from "react";
import { useDirMode, type DirMode } from "@apps/explorer/lib/dir-mode";

/** Named rather than inlined so the probe, the switcher entry and the frame
 *  AppPage.tsx builds around this hook all demonstrably ask for the same
 *  mode. */
export const GIT_COLUMN_MODE = "git";

export interface AppPageGitColumn {
  /** Is the column up at all. */
  open: boolean;
  /** The Doctor row's one way in. */
  openGit: () => void;
  /** The column's own close button, and a drag through its floor. */
  closeGit: () => void;
  /** The folder's borrowed `git` entry, as `useDirMode` resolves it. */
  gitMode: DirMode;
  /** The template's document — the `git` template as the page, the FOLDER as
   *  its subject, in the shape every borrowed-companion frame uses
   *  (Preview.tsx's `sideSrcFor`). `null` while unresolved or absent. */
  gitSrc: string | null;
}

export function useAppPageGitColumn(dir: string): AppPageGitColumn {
  const [open, setOpen] = useState(false);
  // Probed only once the column is asked for: the gate forks a `git
  // rev-parse` per answer (lib/dir-mode caches, but the first one is real
  // work).
  const gitMode = useDirMode(open ? dir : null, GIT_COLUMN_MODE);

  // `useDirMode`'s own state does not update in the SAME render that flips
  // `dir` from null to real — its answer only moves once ITS effect runs, one
  // commit later. On that in-between render `gitMode` still reads the old
  // `{ entry: null, pending: false }` (nothing asked yet, not "asked and
  // said no") — indistinguishable, at that instant, from a folder that
  // genuinely has no git. Closing on that stale read would shut the column
  // the moment it opens, before the probe is even dispatched. `probed` marks
  // the first sighting of `pending`, so a "not offered" verdict only closes
  // the column once the probe has actually gone out and come back.
  const probed = useRef(false);
  useEffect(() => {
    if (!open) {
      probed.current = false;
      return;
    }
    if (gitMode.pending) {
      probed.current = true;
      return;
    }
    // A REJECTED probe (`gitMode.failed`) is not a settled "no git here" — see
    // dir-mode.ts's own header. Closing on it would slam the column shut over a
    // transient network/server error with nothing for the reader to act on;
    // leaving it open lets the template's own iframe show the failure, and
    // closing + reopening (the column's existing close/`openGit` cycle) is
    // already a working retry, because `loadDirModes` evicts a rejection's
    // cache entry on the spot.
    if (probed.current && gitMode.entry === null && !gitMode.failed) setOpen(false);
  }, [open, gitMode.pending, gitMode.entry, gitMode.failed]);

  const gitSrc =
    gitMode.entry && gitMode.entry.path !== null
      ? `/render?path=${encodeURIComponent(gitMode.entry.path)}` +
        `&_file=${encodeURIComponent(dir)}`
      : null;

  return {
    open,
    openGit: () => setOpen(true),
    closeGit: () => setOpen(false),
    gitMode,
    gitSrc,
  };
}
