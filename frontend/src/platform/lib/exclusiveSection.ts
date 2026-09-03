// Only ONE status-bar section's panel is open at a time (D582, user: "we also
// need to ensure only one of the three tabs are active at one time"). Two open
// panels overlapped each other, and two chips both holding the active wash
// (D576) made that "engaged" signal meaningless.
//
// IN PLATFORM, NOT SHELL, and that placement is load-bearing rather than
// incidental: `frontend/scripts/check-boundaries.mjs` forbids platform from
// importing shell, and the two participants straddle the line —
// `DownloadManager.tsx` (the Activity chip) is platform while
// `RepoUpdatesDock.tsx` (Notifications) is shell. Shell may import platform,
// so the shared state lives here and both consume it. It is deliberately NOT
// lifted into `StatusBar.tsx`: the cards reach it as opaque `ReactNode` props
// precisely so the bar never has to know what its children are, and threading
// open-state through it would spend that seam.
//
// A MODULE-LEVEL STORE, not a context provider, for the same reason
// `aiRuntime.ts` is one: there is exactly one status bar per document, the
// participants mount and unmount independently, and a provider would have to
// be installed above all three — which means editing `StatusBar.tsx` or
// `App.tsx` to wrap children whose identities we just said the bar should not
// know.
import { useEffect, useRef } from "react";

/** Lifetime order — Models, Activity, Notifications — which is also the
 *  order `StatusBar.tsx` renders them in and the order that breaks every tie
 *  below. Activity used to be four separate chips (Models, Engines, Jobs)
 *  each with their own entry here; the status-bar merge folded all three into
 *  one "Activity" chip, then a follow-up revision split Models back out into
 *  its own chip (`shell/ModelsDock.tsx`) — Engines stayed folded into
 *  Activity (`platform/ui/DownloadManager.tsx`) since only Models' filled/
 *  outlined dot is load-bearing for the user. So there are three entries now:
 *  one for Models, one for Activity (jobs + engines), one for Notifications.
 *  Named rather than inferred so the tie-break cannot silently change if the
 *  bar's markup is reordered for visual reasons. */
export const SECTION_ORDER = ["models", "activity", "notifications"] as const;
export type SectionKey = (typeof SECTION_ORDER)[number];

interface Entry {
  want: boolean;
  /** Which TICK this section last asked to be open — see `currentTick`. */
  seq: number;
}

const entries = new Map<SectionKey, Entry>();
const closers = new Map<SectionKey, { current: () => void }>();

// A tick shared by everything that happens in one React commit. Every section
// that starts wanting to be open in the SAME commit gets the SAME `seq`, so
// such a clash is a genuine tie and falls to `SECTION_ORDER` — which is what
// makes the awkward cases deterministic rather than dependent on
// effect-execution order. A later commit gets a higher tick, so an ordinary
// user click always beats whatever was already open, whichever section it is.
//
// WHICH TIES ACTUALLY HAPPEN — exactly ONE kind, still (narrowed by D587,
// then D603, then the status-bar merge, then the Models-chip split): two
// sections auto-opening on one poll response, which can only ever be
// Activity vs Notifications. Models can never be one side of this race: its
// own `useAutoExpandOnNew` call is fed an empty `ids` list and every resident
// model rides in as `alsoDrawn` instead (occupancy for its auto-CLOSE-on-
// drain, never the announcing override), so nothing can ever put Models into
// `arrived` — it only ever enters `entries` on a direct user click, and a
// lone click has no sibling to tie against. A running engine is the same
// story for Activity: it rides in as Activity's own `alsoDrawn`, so only a
// genuine job arrival can make Activity want to open at all.
//
// THE RELOAD TIE IS GONE (D603 deleted all fold keys, so nothing wants to be
// open at mount) and so is the FOUR-WAY shape the leading entries used to
// have to arbitrate — with three entries and only two of them (Activity,
// Notifications) capable of auto-opening at all, `SECTION_ORDER` only ever
// has one real tie to break: Activity vs Notifications, decided in Activity's
// favour by this order.
//
// The microtask is what bounds a "commit": React runs layout/passive effects
// synchronously within a commit, before the microtask queue drains.
let tick = 0;
let tickScheduled = false;
function currentTick(): number {
  if (!tickScheduled) {
    tickScheduled = true;
    queueMicrotask(() => {
      tickScheduled = false;
      tick += 1;
    });
  }
  return tick;
}

/** Test-only: forget every section and rewind the tick.
 *
 *  The store is module-level ON PURPOSE (see the top of the file), and bun
 *  runs every test file in one module registry — so a section a sibling
 *  file's test mounted and never unmounted is still in `entries`, wanting to
 *  be open, when this file's tie-break tests run. Which file runs first is
 *  readdir order, and readdir order is what differed between a Mac (green)
 *  and the Linux runner (two tests red on every push). The tests call this
 *  before each case so they arbitrate over their own sections only. */
export function resetExclusiveSectionsForTests(): void {
  entries.clear();
  closers.clear();
  tick = 0;
  tickScheduled = false;
}

function arbitrate(): void {
  const wanting = [...entries.entries()].filter(([, e]) => e.want);
  if (wanting.length <= 1) return;
  // Highest tick wins (most recent request); a tie goes to SECTION_ORDER.
  wanting.sort(
    (a, b) =>
      b[1].seq - a[1].seq ||
      SECTION_ORDER.indexOf(a[0]) - SECTION_ORDER.indexOf(b[0]),
  );
  for (const [key] of wanting.slice(1)) closers.get(key)?.current();
}

/**
 * Declare whether this section WANTS its panel open, and hand over the way to
 * shut it. Whenever more than one section wants it, every loser's `forceClose`
 * is called.
 *
 * `forceClose` must close TRANSIENTLY and must NOT write the user's persisted
 * preference (`autoExpand.ts`'s `forceClose`, which sets the `"closed"`
 * override): closing B because the user opened A is the app arbitrating, not
 * the user choosing, and the D567 guard against persisting a decision the user
 * did not make applies here exactly as it does to auto-open and auto-close.
 *
 * Only ever CLOSES. Nothing here opens a panel, so D580's auto-close cannot
 * hand the screen to a previously-open sibling — a section closing on drain
 * leaves nothing open, which is what the user asked for.
 */
export function useExclusiveSection(
  key: SectionKey,
  wantOpen: boolean,
  forceClose: () => void,
): void {
  // Through a ref so `forceClose`'s changing identity never re-runs the
  // arbitration effect below (it closes over `collapsed`, so it is a fresh
  // closure every render).
  const closeRef = useRef(forceClose);
  closeRef.current = forceClose;

  useEffect(() => {
    closers.set(key, closeRef);
    return () => {
      closers.delete(key);
      entries.delete(key);
    };
  }, [key]);

  useEffect(() => {
    const prev = entries.get(key);
    // A tick is stamped only on the FALSE -> TRUE edge. Re-stamping while a
    // panel merely stays open would let a section that is already open keep
    // out-bidding a sibling the user just clicked.
    if (wantOpen) {
      if (!prev?.want) entries.set(key, { want: true, seq: currentTick() });
    } else {
      entries.set(key, { want: false, seq: 0 });
    }
    arbitrate();
  }, [key, wantOpen]);
}
