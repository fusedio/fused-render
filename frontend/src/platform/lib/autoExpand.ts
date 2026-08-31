// Signal a genuinely NEW item arriving in a folded notification card — and,
// since D574, OPEN that card's own panel so the arrival is actually on screen
// rather than only hinted at. Shared by every status-bar chip that has one —
// shell/ModelsDock.tsx (Models — never actually opens, see its own call
// site), platform/ui/DownloadManager.tsx (Activity — jobs and engines feed
// the one call there, the status-bar merge having folded the now-deleted
// shell/EnginesDock.tsx into it; Models made the same trip and then moved
// back out into its own chip) and shell/RepoUpdatesDock.tsx (Notifications)
// — since each needs the exact same wiring around the same pure decision
// (jobs.ts `trackSeenIds`): remember which ids were visible last render, and
// notice when the current set contains one that wasn't.
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
  /** Transient, NEVER persisted (see this file's header): true from the moment
   *  a new id lands while the section is collapsed until the panel is
   *  dismissed. */
  autoOpen: boolean;
  /** The mirror of `autoOpen` (D580, user: "after a job finishes, ensure we
   *  close the jobs popover if no jobs left"): true once EVERY row the panel
   *  draws has drained away while it was open (`ids` plus `alsoDrawn` — see
   *  that option, and code review finding 1). Equally transient — the
   *  saved preference is untouched, so the section reverts to whatever the
   *  user themselves last chose.
   *
   *  Together these two make the caller's rule
   *  `open = autoClose ? false : !collapsed || autoOpen`, and they are
   *  mutually exclusive by construction (one `Override`, not two booleans). */
  autoClose: boolean;
  /** Drop whichever override is standing, handing the panel back to the
   *  caller's own `collapsed` — the click / outside pointer-down / Escape path.
   *  There is no indicator to clear alongside it: D588 deleted `.dl-new-dot`
   *  app-wide, so visibility is the only thing this hook has ever held since.
   *  Stable identity, so it is safe in an effect's dependency list. */
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
/**
 * Per-caller shaping of the two halves. There is no `neverClose` any more, and
 * that removal is the point rather than a tidy-up: a caller that wanted its
 * drain ignored actually wanted a drain of ITS OWN SOURCE not to be mistaken
 * for the panel emptying, which is a question about what the panel DRAWS, not
 * a switch to flip. `alsoDrawn` answers it directly, so the two remaining
 * knobs are about genuinely different things:
 *
 *  - Activity's engine rows (`alsoDrawn`, status-bar merge) must never open
 *    the panel on their own — an engine coming up is state, not news — but
 *    they MUST still count toward the drain that closes it: "closing is not
 *    opening" is the same argument the (now-deleted) standalone Engines chip
 *    made for its own drain-close behaviour with the since-removed
 *    `neverOpen` flag. Jobs are the one source in Activity that stays in
 *    `ids`. Models' own resident-model rows (`shell/ModelsDock.tsx`) use this
 *    same knob for the identical reason, one level up: `ids` there is always
 *    `[]`, so nothing can ever announce, and every model rides in as
 *    `alsoDrawn` purely for the drain-close (D580's "close when the last
 *    model unloads").
 *  - Notifications (`alsoDrawn` = its failure rows) draws TWO row sources but
 *    may only be opened by one of them: a repo falling behind is news (D574), a
 *    background failure must never throw a panel over the page (D586). Feeding
 *    the failures in as `alsoDrawn` keeps the "never opens for a failure" half
 *    STRUCTURAL — they are not in `ids`, so there is no path from a failure to
 *    `setOverride("open")` at all — while still letting them hold the panel
 *    open against a repo-row drain.
 */
export interface AutoExpandOptions {
  /** MORE ROWS THIS PANEL DRAWS, which count for occupancy but never announce.
   *
   *  THE DEFECT THIS FIXES (code review 2026-08-28, finding 1). The drain gate
   *  below has always meant "the panel is genuinely empty, so there is nothing
   *  left to keep it open for" — but it could only ever see `ids`, and both
   *  real panels draw rows from TWO sources. Jobs draws the scheduled queue's
   *  rows above its job rows; Notifications draws failed jobs under its repo
   *  rows. So the last download finishing force-closed the Jobs panel over a
   *  live turn's rows — including that turn's only ✕ — and pressing Update on
   *  the last repo row force-closed Notifications over failure rows the user
   *  was reading.
   *
   *  Fed the UNION, the gate means what it says again. The seen set is built
   *  from the union too, so the transition still fires exactly once whichever
   *  source drains last, instead of being missed when the other source empties
   *  a tick later.
   *
   *  IDENTITIES MUST BE DISJOINT ACROSS THE SOURCES, since a collision would
   *  put a not-yet-seen row in `prev` and silently swallow its arrival. Every
   *  call site prefixes per source (`job:` / `queue:` / `repo:`) rather than
   *  relying on two id namespaces happening not to overlap. */
  alsoDrawn?: readonly string[];
}

const NOTHING_ELSE: readonly string[] = [];

export function useAutoExpandOnNew(
  ids: readonly string[],
  collapsed: boolean,
  ready = true,
  { alsoDrawn = NOTHING_ELSE }: AutoExpandOptions = {},
): AutoExpandState {
  const seenRef = useRef<Set<string> | null>(null);
  const [override, setOverride] = useState<Override>(null);

  useEffect(() => {
    // Nothing is knowable before the first response — not even "the set is
    // empty" — so hold off entirely rather than seeding from a placeholder.
    if (!ready) return;
    // EVERYTHING THE PANEL DRAWS, for occupancy; `ids` alone for announcing.
    // See `alsoDrawn` above for why these are two lists and not one.
    const drawn = alsoDrawn.length === 0 ? ids : [...ids, ...alsoDrawn];
    const prev = seenRef.current;
    if (prev === null) {
      seenRef.current = new Set(drawn);
      return;
    }
    const { seen } = trackSeenIds(drawn, prev);
    seenRef.current = seen;
    // `trackSeenIds`'s own `hasNew` is deliberately NOT used: it answers "is
    // anything in the union new", and an arrival among `alsoDrawn` must not
    // announce. Restricting the test to `ids` is what keeps the announce half
    // narrow while the occupancy half stays wide.
    const arrived = ids.some((id) => !prev.has(id));

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
    // construction, not by luck: `arrived` means `ids` contains something, and
    // `ids` is a subset of `drawn`, so the drain test below
    // (`drawn.length === 0`) cannot also be true. The
    // early return makes that ordering explicit rather than leaving it to be
    // re-derived, so a new job landing as the last one finishes auto-OPENS
    // and there is no close-then-open flash.
    if (arrived) {
      // Announce only what the user cannot already see. Setting `"open"` here
      // is also what CLEARS a standing `"closed"` override, which is the half
      // the old `collapsed`-only test could never reach. There is no dot to
      // set any more: D588 replaced every "something NEW" indicator with a
      // single per-chip circle for "is there anything here", which the chip
      // derives from its own list and needs no hook state for.
      if (!panelOpen) setOverride("open");
      return;
    }

    // DRAINED: EVERYTHING THE PANEL DRAWS went non-empty -> empty (`drawn`, not
    // `ids` — see `alsoDrawn`, which is the whole of code review finding 1).
    // `trackSeenIds` returns a set built only from what was passed in, so
    // `prev` shrinks with the union and this fires exactly once on the
    // transition — the tick after, `prev.size` is already 0. A section that was
    // ALREADY empty is therefore never touched, which is what keeps this from
    // fighting a user who deliberately opened an idle section to look at it.
    //
    // Gated on `panelOpen` because there is nothing to close otherwise, and
    // because leaving a `"closed"` override standing on an already-closed
    // section would make the next chip click spend itself clearing it instead
    // of opening the panel.
    if (prev.size > 0 && drawn.length === 0 && panelOpen) {
      setOverride("closed");
    }
  });

  // A change to the SAVED preference is the user speaking directly, which
  // outranks any override this hook is holding — so drop it and let their
  // choice through, in both directions.
  useEffect(() => {
    setOverride(null);
  }, [collapsed]);

  const acknowledge = useCallback(() => {
    setOverride(null);
  }, []);

  const forceClose = useCallback(() => {
    setOverride("closed");
  }, []);

  // ONE `Override`, projected as two mutually-exclusive booleans, so the
  // caller's rule (`autoClose ? false : !collapsed || autoOpen`) cannot land in
  // a state where both are true. There is no third value to return: D577's
  // defect 2 was `Activity 1  46% ●` — a newness dot beside an already-open
  // panel — and D588 removed the dot itself rather than the coincidence, so
  // visibility is now the whole of this hook's output.
  return {
    autoOpen: override === "open",
    autoClose: override === "closed",
    acknowledge,
    forceClose,
  };
}
