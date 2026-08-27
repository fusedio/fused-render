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

// The panel's visibility can be steered by this hook in EITHER direction, and
// both directions are transient — a temporary override sitting on top of the
// caller's own persisted `collapsed` preference, never a write to it (D567's
// standing guard, restated in D574 and D580). `null` means "no opinion, the
// saved preference decides".
type Override = null | "open" | "closed";

export interface AutoExpandState {
  /** An unacknowledged arrival the user cannot currently SEE — drawn as the
   *  chip's quiet `.dl-new-dot`. Suppressed while this section's panel is
   *  showing the arrival itself (D577): a dot beside an open panel listing
   *  the very thing it is pointing at announces news the user is already
   *  looking at. Its real case is an arrival that never got a panel. */
  hasNew: boolean;
  /** Transient, NEVER persisted (see this file's header): true from the moment
   *  a new id lands while the section is collapsed until the panel is
   *  dismissed. */
  autoOpen: boolean;
  /** The mirror of `autoOpen` (D580, user: "after a job finishes, ensure we
   *  close the jobs popover if no jobs left"): true once the row list has
   *  drained to empty while the panel was open. Equally transient — the
   *  saved preference is untouched, so the section reverts to whatever the
   *  user themselves last chose.
   *
   *  Together these two make the caller's rule
   *  `open = autoClose ? false : !collapsed || autoOpen`, and they are
   *  mutually exclusive by construction (one `Override`, not two booleans). */
  autoClose: boolean;
  /** Drop whichever override is standing and clear the dot — the caller's own
   *  click / outside pointer-down / Escape path. Stable identity, so it is
   *  safe in an effect's dependency list. */
  acknowledge: () => void;
  /** Force the panel shut TRANSIENTLY, leaving the saved preference alone —
   *  the same `"closed"` override a drain sets. Its caller is the one-panel-
   *  at-a-time arbiter (`exclusiveSection.ts`, D582): closing this section
   *  because the user opened a different one is the app deciding, not the
   *  user, so it must not persist. Stable identity. */
  forceClose: () => void;
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
  const [override, setOverride] = useState<Override>(null);

  useEffect(() => {
    // Nothing is knowable before the first response — not even "the set is
    // empty" — so hold off entirely rather than seeding from a placeholder.
    if (!ready) return;
    const prev = seenRef.current;
    if (prev === null) {
      seenRef.current = new Set(ids);
      return;
    }
    const { seen, hasNew: arrived } = trackSeenIds(ids, prev);
    seenRef.current = seen;

    // EFFECTIVE visibility, not the persisted flag — the caller's own `open`
    // rule, computed once here because BOTH branches below need it (D584
    // review finding 1). Using bare `collapsed` for the arrival branch was a
    // real bug with a permanent consequence: on a default install (no stored
    // key, so `collapsed === false`) a section that had auto-CLOSED on drain
    // held `override === "closed"` while `collapsed` stayed false, so the next
    // arrival satisfied neither `collapsed` nor anything that clears the
    // override — no panel, no dot, for the rest of the session, and the same
    // for any section D582's arbiter had force-closed. That is D574 defeated
    // outright, so visibility is derived in one place and both branches read
    // it.
    const panelOpen = override === "closed" ? false : !collapsed || override === "open";

    // AN ARRIVAL WINS over a drain in the same tick (D580) — and it wins by
    // construction, not by luck: `arrived` means `ids` contains something,
    // so the drain test below (`ids.length === 0`) cannot also be true. The
    // early return makes that ordering explicit rather than leaving it to be
    // re-derived, so a new job landing as the last one finishes auto-OPENS
    // and there is no close-then-open flash.
    if (arrived) {
      // Announce only what the user cannot already see. Setting `"open"` here
      // is also what CLEARS a standing `"closed"` override, which is the half
      // the old `collapsed`-only test could never reach.
      if (!panelOpen) {
        setHasNew(true);
        setOverride("open");
      }
      return;
    }

    // DRAINED: the list went non-empty -> empty. `trackSeenIds` returns a set
    // built only from `currentIds`, so `prev` shrinks with the list and this
    // fires exactly once on the transition — the tick after, `prev.size` is
    // already 0. A section that was ALREADY empty is therefore never touched,
    // which is what keeps this from fighting a user who deliberately opened an
    // idle section to look at it.
    //
    // Gated on `panelOpen` because there is nothing to close otherwise, and
    // because leaving a `"closed"` override standing on an already-closed
    // section would make the next chip click spend itself clearing it instead
    // of opening the panel.
    if (prev.size > 0 && ids.length === 0 && panelOpen) {
      setHasNew(false);
      setOverride("closed");
    }
  });

  // A change to the SAVED preference is the user speaking directly, which
  // outranks any override this hook is holding — so drop it and let their
  // choice through, in both directions.
  useEffect(() => {
    setHasNew(false);
    setOverride(null);
  }, [collapsed]);

  const acknowledge = useCallback(() => {
    setHasNew(false);
    setOverride(null);
  }, []);

  const forceClose = useCallback(() => {
    setHasNew(false);
    setOverride("closed");
  }, []);

  // The dot and the auto-opened panel are never shown together (D577 defect 2:
  // `Activity 1  46% ●` with the panel already open). Suppressed rather than
  // never set, so dismissing the panel (`acknowledge`, which clears both) does
  // not leave a dot behind for something the user just chose to close.
  return {
    hasNew: hasNew && override !== "open",
    autoOpen: override === "open",
    autoClose: override === "closed",
    acknowledge,
    forceClose,
  };
}
