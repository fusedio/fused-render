// The app page's right-hand pane is ONE column with a tab strip — Claude | Git
// — the explorer's file sidebar's own switcher (apps/explorer/SideChrome's
// SideTabs), not two columns side by side (owner, 2026-11-25: "in projects
// page git and claude open as different sidebars, instead they should be same
// window but with a tabbar like in explorer view"). This hook is that strip's
// state: which tab is up, the borrowed `git` probe against the app FOLDER, and
// the git template's `src`.
//
// Extracted out of AppPage.tsx's own body for the same reason
// useAppPageSnapshot.ts was (see that file's own header, and
// AppPage.test.tsx's): AppPage.tsx itself has no render-test precedent, so the
// piece of it with real behaviour to pin gets its own hook, driven through the
// REAL `useDirMode` fetches (stubbed only at the network boundary).
//
// THE PROBE runs only while the pane is in the tree (`enabled` — the caller
// passes the pane's mounted state, so the Git tab keeps its frame through the
// close slide): a page whose pane is shut asks nothing. While it is in flight
// the Git tab spins; settled with no git here, the tab is drawn disabled with
// its reason (the explorer strip's posture: the strip always shows the whole
// closed set, never shrinks to one tab).
//
// THE WAYS ONTO GIT: the strip itself, App Doctor's "Open in git" row and the
// rail's Git card (`showTab`; the caller also shows the pane). The way back is
// the strip — OR the fall-back below: settled, and this folder does not offer
// git at all (no repository, or the gate said no), so a Git tab that was up
// hands the column back to Claude. A REJECTED probe is not that answer
// (`failed`): the tab stays, and its body says the check failed.
//
// The tab picked ON THE STRIP is remembered with the explorer's own
// (lib/side-tab-store, memory only): Git picked on a file is still the tab
// here, and back. A door (`showTab`) changes this page only.
import { useCallback, useEffect, useRef, useState } from "react";
import { useDirMode, type DirMode } from "@apps/explorer/lib/dir-mode";
import { getSideTab, setSideTab } from "@apps/explorer/lib/side-tab-store";

/** The two tabs, named as the explorer names its companions, so the remembered
 *  tab (side-tab-store) means the same thing on both surfaces. */
export const CLAUDE_TAB = "claude";
export const GIT_TAB = "git";
export type PaneTab = typeof CLAUDE_TAB | typeof GIT_TAB;

export interface AppPagePaneTab {
  /** The tab on screen. */
  tab: PaneTab;
  /** A press on the strip — the reader's own pick, so it is remembered. */
  selectTab: (tab: PaneTab) => void;
  /** A door putting a tab up (a ring, the rail's +, the Doctor row, the
   *  rail's Git card): local only — side-tab-store holds only what the reader
   *  picked on a strip, so a door here never rewrites the explorer's tab. */
  showTab: (tab: PaneTab) => void;
  /** The folder's borrowed `git` entry, as `useDirMode` resolves it. */
  gitMode: DirMode;
  /** The folder's `claude` entry — only its icon is read. */
  claudeMode: DirMode;
  /** The template's document — the `git` template as the page, the FOLDER as
   *  its subject, in the shape every borrowed-companion frame uses
   *  (Preview.tsx's `sideSrcFor`). `null` while unresolved or absent. */
  gitSrc: string | null;
}

function rememberedTab(): PaneTab {
  return getSideTab() === GIT_TAB ? GIT_TAB : CLAUDE_TAB;
}

export function useAppPagePaneTab(dir: string, enabled: boolean): AppPagePaneTab {
  const [tab, setTab] = useState<PaneTab>(rememberedTab);
  const gitMode = useDirMode(enabled ? dir : null, GIT_TAB);
  const claudeMode = useDirMode(enabled ? dir : null, CLAUDE_TAB);

  // `useDirMode` answers ABSENT for the one commit between `enabled` turning
  // on and its own effect setting the pending placeholder — the same shape as
  // a settled "no git here". Only a probe this hook has SEEN in flight may
  // send the column back to Claude.
  const probed = useRef(false);
  useEffect(() => {
    if (!enabled) {
      probed.current = false;
      return;
    }
    if (gitMode.pending) {
      probed.current = true;
      return;
    }
    if (tab === GIT_TAB && probed.current && gitMode.entry === null && !gitMode.failed) {
      setTab(CLAUDE_TAB);
    }
  }, [enabled, tab, gitMode.pending, gitMode.entry, gitMode.failed]);

  const selectTab = useCallback((next: PaneTab) => {
    setSideTab(next);
    setTab(next);
  }, []);
  const showTab = useCallback((next: PaneTab) => setTab(next), []);

  const gitSrc =
    gitMode.entry && gitMode.entry.path !== null
      ? `/render?path=${encodeURIComponent(gitMode.entry.path)}` +
        `&_file=${encodeURIComponent(dir)}`
      : null;

  return { tab, selectTab, showTab, gitMode, claudeMode, gitSrc };
}
