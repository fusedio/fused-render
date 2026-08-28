// Only ONE status-bar section's panel is open at a time (D582, user: "we also
// need to ensure only one of the three tabs are active at one time"). Two open
// panels overlapped each other, and two chips both holding the active wash
// (D576) made that "engaged" signal meaningless.
//
// IN PLATFORM, NOT SHELL, and that placement is load-bearing rather than
// incidental: `frontend/scripts/check-boundaries.mjs` forbids platform from
// importing shell, and the three participants straddle the line —
// `DownloadManager.tsx` is platform while `ModelsDock.tsx` and
// `RepoUpdatesDock.tsx` are shell. Shell may import platform, so the shared
// state lives here and all three consume it. It is deliberately NOT lifted
// into `StatusBar.tsx`: the cards reach it as opaque `ReactNode` props
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

/** Lifetime order — Models, Engines, Jobs, Notifications — which is also the
 *  order `StatusBar.tsx` renders them in and the order that breaks every tie
 *  below. Engines sits beside Models (D591) because both report what is
 *  RUNNING RIGHT NOW, where Jobs and Notifications are transient work that
 *  appears and resolves.
 *  Named rather than inferred so the tie-break cannot silently change if the
 *  bar's markup is reordered for visual reasons. */
export const SECTION_ORDER = ["models", "engines", "jobs", "notifications"] as const;
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
// WHICH TIES ACTUALLY HAPPEN — exactly ONE kind, now (narrowed by D587, then
// again by D603):
//  - TWO SECTIONS AUTO-OPENING on one poll response, which since D587 can only
//    ever be Jobs vs Notifications. Models and Engines pass `neverOpen`
//    (`autoExpand.ts`), so they have no auto-open path at all.
//
// THE RELOAD TIE IS GONE. It used to be the other case, and it was the
// justification for `SECTION_ORDER`'s leading entry: two or more sections'
// PERSISTED preferences saying open on a reload, which Models could win and
// should, since a saved preference is the user's own choice and D587 forbids
// Models AUTO-opening rather than being open. D603 deleted all four fold keys,
// so nothing wants to be open at mount and that tie is unreachable.
//
// SO THE "MODELS FIRST" HALF OF THE ORDER NOW RESTS ON NOTHING, and this
// comment says so rather than inventing a replacement rationale (code review
// 2026-08-28, finding 15). The order is kept as-is because it mirrors
// `StatusBar.tsx`'s lifetime ordering, which is a real and separately-argued
// rule, and because the only live tie is decided by the Jobs-before-
// Notifications half — but if a future section needs to win a tie against Jobs,
// there is no prior decision here to argue with.
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
