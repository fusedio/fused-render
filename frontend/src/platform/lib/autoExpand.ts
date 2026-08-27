// Signal a genuinely NEW item arriving in a folded notification card, WITHOUT
// opening anything (D567 follow-up to D562, code review finding #4). Shared by
// all three status-bar sections that track their own seen-id set —
// platform/ui/DownloadManager.tsx's jobs/downloads card, shell/RepoUpdatesDock.tsx's
// repo-updates card, and shell/ModelsDock.tsx's resident-models panel — since
// each needs the exact same wiring around the same pure decision (jobs.ts
// `trackSeenIds`): remember which ids were visible last render, and notice
// when the current set contains one that wasn't.
//
// IT USED TO FORCE THE CARD OPEN (`persist(false); setCollapsed(false)`), and
// that is exactly the bug this whole feature exists to fix, recreated one
// level down: a user collapses the bar to reclaim space, opens a page like
// the Claude template, and the first background job a page spawns popped a
// floating PANEL over that page uninvited — worse, it PERSISTED the
// expansion to localStorage, so the panel came back open on the next reload
// even with nothing new left to show. `StatusBar.tsx`'s own justification for
// letting a panel overlay the page ("it is user-initiated") was never true
// here. So this hook no longer touches `collapsed` or `persist` at all — it
// only answers "is there something the user has not looked at yet", as a
// plain boolean the caller draws as a quiet dot on the chip (`.dl-new-dot`),
// never as a forced expansion. Opening the panel — the user's own click — is
// what clears it.
//
// Deliberately its OWN file rather than folded into lib/hooks.ts: hooks.ts
// imports router.ts for `NAV_EVENT`, and router.ts's module-init code reads
// `location` at import time — fine for the shell-side call sites hooks.ts
// already has, but DownloadManager.test.tsx (platform-side) mounts
// DownloadManagerView with no `location`/`window`/`history` stub at all
// (unlike RepoUpdatesDock.test.tsx, which installs one specifically to get
// router.ts through its own init). Pulling this hook in through hooks.ts
// would drag that whole chain into a test file that has never needed it.
// This file imports nothing but react and jobs.ts's pure `trackSeenIds`.
import { useEffect, useRef, useState } from "react";
import { trackSeenIds } from "@platform/lib/jobs";

// The FIRST render seeds `seen` from whatever is already there rather than
// treating it as a wave of arrivals — an app opened onto an already-running
// job (or an already-behind repo) should not mark the card "new" on load;
// "new" means "arrived while you were looking away", not "already there
// before mount".
//
// `ids` is expected to be the exact list the card is about to RENDER as rows
// (post every other filter — vanished-on-success, dismissed, drawn-elsewhere)
// so "new" means "a row that is about to appear", not merely "a record the
// server still knows about".
//
// Returns whether an id arrived while `collapsed` was true and has not been
// acknowledged since — cleared the moment `collapsed` goes false (the user's
// own click), never by this hook reaching in and flipping it. Only counted
// while collapsed in the first place: an arrival landing while the panel is
// ALREADY open is something the user is already looking at, not news.
export function useAutoExpandOnNew(ids: readonly string[], collapsed: boolean): boolean {
  const seenRef = useRef<Set<string> | null>(null);
  const [hasNew, setHasNew] = useState(false);

  useEffect(() => {
    if (seenRef.current === null) {
      seenRef.current = new Set(ids);
      return;
    }
    const { seen, hasNew: arrived } = trackSeenIds(ids, seenRef.current);
    seenRef.current = seen;
    if (arrived && collapsed) setHasNew(true);
  });

  // Expanding — for any reason, not only in response to this dot — is the
  // acknowledgement. Nothing here ever sets `collapsed` itself.
  useEffect(() => {
    if (!collapsed) setHasNew(false);
  }, [collapsed]);

  return hasNew;
}
