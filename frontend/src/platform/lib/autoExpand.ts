// Un-collapse a folded notification card when a genuinely NEW item shows up
// (D562 follow-up, user call: "we can make the notifications 'un collapse'
// when a new one comes"). Shared by both notification cards —
// platform/ui/DownloadManager.tsx's jobs/downloads card and
// shell/RepoUpdatesDock.tsx's repo-updates card — since both need the exact
// same wiring around the same pure decision (jobs.ts `trackSeenIds`):
// remember which ids were visible last render, and when the current set
// contains one that wasn't, open the card the same way a user's own toggle
// would (persisted, not just local state — `persist` is each card's own
// localStorage write).
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
import { useEffect, useRef, type Dispatch, type SetStateAction } from "react";
import { trackSeenIds } from "@platform/lib/jobs";

// The FIRST render seeds `seen` from whatever is already there rather than
// treating it as a wave of arrivals — an app opened onto an already-running
// job (or an already-behind repo) should not force the card open on load;
// "new" means "arrived while you were looking at it", not "already there
// before mount".
//
// `ids` is expected to be the exact list the card is about to RENDER as rows
// (post every other filter — vanished-on-success, dismissed, drawn-elsewhere)
// so "new" means "a row that is about to appear", not merely "a record the
// server still knows about".
export function useAutoExpandOnNew(
  ids: readonly string[],
  collapsed: boolean,
  setCollapsed: Dispatch<SetStateAction<boolean>>,
  persist: (collapsed: boolean) => void,
): void {
  const seenRef = useRef<Set<string> | null>(null);
  useEffect(() => {
    if (seenRef.current === null) {
      seenRef.current = new Set(ids);
      return;
    }
    const { seen, hasNew } = trackSeenIds(ids, seenRef.current);
    seenRef.current = seen;
    if (hasNew && collapsed) {
      persist(false);
      setCollapsed(false);
    }
  });
}
