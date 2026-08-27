// Signal a genuinely NEW item arriving in a folded notification card — and,
// since D574, OPEN that card's own panel so the arrival is actually on screen
// rather than only hinted at. Shared by all three status-bar sections that
// track their own seen-id set — platform/ui/DownloadManager.tsx's
// jobs/downloads card, shell/RepoUpdatesDock.tsx's repo-updates card, and
// shell/ModelsDock.tsx's resident-models panel — since each needs the exact
// same wiring around the same pure decision (jobs.ts `trackSeenIds`):
// remember which ids were visible last render, and notice when the current
// set contains one that wasn't.
//
// D567 STOPPED IT OPENING ANYTHING; D574 PUTS THE OPEN BACK, by explicit user
// request: "when we have something new, always show the notification. don't
// keep no activity displayed" — reported with the Activity panel open reading
// `No activity` while the new-item dot sat over on Models. A quiet dot makes
// the user hunt for which of three sections actually received the thing, and
// the section they happened to have open is free to be truthfully, uselessly
// empty at the same time.
//
// THE GUARD FROM D567 SURVIVES, though, because it was the real defect and it
// is not the same thing as opening: the old code did `persist(false)` as well
// as `setCollapsed(false)`, writing a FORCED expansion back to localStorage as
// though the user had chosen it, so the panel returned open on the next reload
// with nothing new left to show. `autoOpen` here is transient React state and
// nothing else — the caller ORs it over its own persisted `collapsed` flag and
// never saves it, so a section that auto-opens for one arrival goes straight
// back to whatever the user themselves last chose.
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
import { useCallback, useEffect, useRef, useState } from "react";
import { trackSeenIds } from "@platform/lib/jobs";

export interface AutoExpandState {
  /** An unacknowledged arrival the user cannot currently SEE — drawn as the
   *  chip's quiet `.dl-new-dot`. Suppressed while this section's panel is
   *  showing the arrival itself (D577): a dot beside an open panel listing
   *  the very thing it is pointing at announces news the user is already
   *  looking at. Its real case is an arrival that never got a panel. */
  hasNew: boolean;
  /** Transient, NEVER persisted (see this file's header): true from the moment
   *  a new id lands while the section is collapsed until the panel is
   *  dismissed. The caller treats the panel as open when `!collapsed ||
   *  autoOpen`, and leaves its own saved preference alone. */
  autoOpen: boolean;
  /** Drop the auto-open and clear the dot — the caller's own "close" path
   *  (the chip's click, an outside pointer-down, Escape). Stable identity, so
   *  it is safe in an effect's dependency list. */
  acknowledge: () => void;
}

// The FIRST render seeds `seen` from whatever is already there rather than
// treating it as a wave of arrivals — an app opened onto an already-running
// job (or an already-behind repo) should not pop its panel open on load;
// "new" means "arrived while you were looking away", not "already there
// before mount".
//
// `ids` is expected to be the exact list the card is about to RENDER as rows
// (post every other filter — vanished-on-success, dismissed, drawn-elsewhere)
// so "new" means "a row that is about to appear", not merely "a record the
// server still knows about".
//
// Only arrivals landing while `collapsed` is true count, which is both the
// old dot rule and exactly the condition worth opening on: an item arriving
// into an ALREADY-open panel is something the user is looking at already.
// `ready` — has the caller's own fetch actually resolved yet? DEFAULTS TO
// TRUE only so a caller that genuinely cannot tell keeps the old behaviour;
// every real call site passes a live flag, because without one this hook
// announced a false arrival on every page load (D574 bug 2). The seeding
// effect below has no dependency array (it must run after every render to
// diff the list), so on the FIRST render — before any response has landed —
// `ids` is the caller's initial `[]` and the seen set was seeded EMPTY. Every
// pre-existing item then read as an arrival the moment the data arrived,
// which is a dot and (since D574) an auto-opened panel on a plain page load,
// the exact opposite of this hook's stated intent. Seeding from the first
// render THAT HAS DATA rather than the first render full stop fixes it, and
// `ready` rather than `ids.length > 0` is what distinguishes "nothing here
// yet" from "genuinely nothing" — the latter must still let the very first
// real arrival through as news.
export function useAutoExpandOnNew(
  ids: readonly string[],
  collapsed: boolean,
  ready = true,
): AutoExpandState {
  const seenRef = useRef<Set<string> | null>(null);
  const [hasNew, setHasNew] = useState(false);
  const [autoOpen, setAutoOpen] = useState(false);

  useEffect(() => {
    // Nothing is knowable before the first response — not even "the set is
    // empty" — so hold off entirely rather than seeding from a placeholder.
    if (!ready) return;
    if (seenRef.current === null) {
      seenRef.current = new Set(ids);
      return;
    }
    const { seen, hasNew: arrived } = trackSeenIds(ids, seenRef.current);
    seenRef.current = seen;
    if (arrived && collapsed) {
      setHasNew(true);
      setAutoOpen(true);
    }
  });

  // Expanding by hand acknowledges too — the user is looking at the rows, so
  // neither the dot nor a pending auto-open has anything left to announce.
  useEffect(() => {
    if (!collapsed) {
      setHasNew(false);
      setAutoOpen(false);
    }
  }, [collapsed]);

  const acknowledge = useCallback(() => {
    setHasNew(false);
    setAutoOpen(false);
  }, []);

  // The dot and the auto-opened panel are never shown together (D577 defect 2:
  // `Activity 1  46% ●` with the panel already open). Suppressed rather than
  // never set, so dismissing the panel (`acknowledge`, which clears both) does
  // not leave a dot behind for something the user just chose to close.
  return { hasNew: hasNew && !autoOpen, autoOpen, acknowledge };
}
