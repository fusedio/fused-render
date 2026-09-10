// THE CONTROL STRIP'S WORDS FIT OR FOLD — T:7554-7595 `annFitStrip`, the same
// collision detection `fit.ts` does for the composer row one pane over.
//
// COLLISION-DETECTED, NEVER A BREAKPOINT (Akshil, 2026-08-19: a fixed width
// dropped the words while there was still space for them). The stylesheet does
// the rest through the `--c-annlbl` token every `.c-lbl` span already reads.
//
// This is what QA #2 was missing: at 1280px with the pane taking most of the
// width the chat column is ~308px, and `Screenshot · Comment · Annotate` at full
// labels measured 308.3px with its right edge 8px past the viewport — legacy has
// rendered the same row icon-only since 2026-08-19.
//
// ── THE NATURAL WIDTH IS MEASURED ONCE AND CACHED (P3R1-1) ──────────────────
//
// The first port re-derived the verdict from scratch on every delivery: take
// `.tight` off, ask the row what it needs, put it back if it does not fit. Two
// things were wrong with that, and dragging the sidebar divider showed both
// (owner, 2026-09-10: "Screenshot/Comment/Annotate glitch in and out; after
// several resizes they stay collapsed even with room").
//
//   * IT WROTE THE DOM ON EVERY RESIZE FRAME. A drag delivers a ResizeObserver
//     callback per frame, and each one removed a class, forced a synchronous
//     layout, and put it back. Any frame where the browser got between those
//     two writes — a nested observer delivery, an interleaved mutation record,
//     the RO loop hitting its depth limit and painting the intermediate state —
//     painted the labels for that frame. That is the glitching.
//   * THE MEASUREMENT WAS TAKEN OF A ROW THAT COULD ALREADY BE SQUEEZED. The
//     seats' widths come off `getBoundingClientRect()`, and a flex row that
//     overflows has had its items shrunk — so the "natural" need measured at a
//     narrow width comes back SMALLER than the labels really want. Cached the
//     wrong way round, that is a strip that stays folded at a width where the
//     words fit.
//
// So the natural width — what the row needs with every word ON — is a property
// of the CONTENT, not of the box, and it is measured once per content
// generation and kept. Widths then decide nothing but a comparison against that
// constant, which makes the verdict a pure function of one number and removes
// the feedback loop entirely: no write per frame, nothing to flicker. The cache
// is thrown away by the same MutationObserver that already watched the children
// (a seat swaps faces, the picker slides in, a label grows), so a generation
// only ever describes one set of seats.
//
// ONE READING IS ENOUGH FOR THE SEATS THAT FOLD, and that is why the cache is
// safe: `.c-anncta`'s buttons are `white-space: nowrap`, so its automatic
// minimum size is its own content and the group CANNOT be squeezed below the
// width its words want. What a squeezed row can under-report is its other
// members (the ← Chats seat's text, a title) — and folding the three seats'
// labels was never going to buy those any room anyway.
//
// ── AND THE HYSTERESIS RUNS ONE WAY (P3R1-1) ────────────────────────────────
//
// `FIT_HYSTERESIS` is spent on the FOLD and never on the return: the row folds
// only once it is over by more than a few px, and unfolds the moment the words
// fit at all. The invariant that buys is the one the owner asked for — `.tight`
// is on ONLY while the labels genuinely do not fit — while the band still
// absorbs the sub-pixel wobble that made a strip sitting 2px inside its own
// boundary (measured live on :1915: 380px of row against a 378px need) flip on
// every jitter. A couple of px of tolerated overflow is paid by the ⋮'s own 16px
// of row padding; a strip that folds and unfolds under the reader's hand is not.
//
// TWO OBSERVERS, ONE VERDICT, and the split is the load-bearing part:
//   * ResizeObserver on the row — the box changes (the divider drags, the window
//     resizes, a narrow layout flips).
//   * MutationObserver on the row's CHILDREN — the content changes width with
//     the box standing still. Deliberately NOT on the row itself for
//     attributes: this function writes the row's own class list, and observing
//     the node it writes would make every verdict schedule the next one, forever
//     (T:7551). A second childList-only observer on the row re-seats the child
//     observers when React adds or removes one.
import { useCallback, useRef } from "react";

import { measureRowNeed } from "./fit";

/** The px of overflow a strip is allowed to carry before it folds. Spent on the
 *  FOLD only — see the note above. */
export const FIT_HYSTERESIS = 3;

export interface StripFit {
  /** Re-decide `.tight` for `row` against the cached natural width. */
  run(row: HTMLElement | null | undefined): void;
  /** The content changed: the cached natural width is stale. */
  invalidate(): void;
  /** The cached natural width, or `null` while it has never been measurable.
   *  Exposed for the test, which is what proves the cache is a cache. */
  natural(): number | null;
}

/**
 * T:7566 — the strip's decider, with the natural width it remembers. `need` is
 * injected so a test can prove the verdict without a layout engine.
 */
export function createStripFit(
  need: (row: HTMLElement) => number = measureRowNeed,
): StripFit {
  let natural: number | null = null;
  return {
    invalidate(): void {
      natural = null;
    },
    natural(): number | null {
      return natural;
    },
    run(row: HTMLElement | null | undefined): void {
      if (!row) return;
      // NOT LAID OUT YET IS NOT A VERDICT. The first `run()` fires from the ref
      // callback during commit, when the row can still measure `clientWidth: 0`
      // — and any `need > 0` against that stamps `.tight`, which the
      // ResizeObserver's first real delivery then takes back off, so the strip
      // painted icon-only for one frame on every mount. A row with no width has
      // no answer to give, so the class is left exactly as it is (unmeasurable
      // is not "it fits") and the observers deliver the first verdict that means
      // anything.
      const box = row.clientWidth;
      if (!box) return;
      const tight = row.classList.contains("tight");
      // MEASURED WITH THE WORDS ON, and only when the answer is not already
      // known: a folded row measures the icons, and a strip measured folded
      // never unfolds again. Both halves of the probe run synchronously inside
      // an observer callback, which fires before paint, so it never shows — and
      // it now runs once per content generation rather than once per frame.
      if (natural === null) {
        if (tight) row.classList.remove("tight");
        natural = need(row);
        if (tight) row.classList.add("tight");
      }
      // Fold only once it is over by more than the band; unfold the moment the
      // words fit at all. `.tight` therefore never outlives the overflow that
      // earned it.
      if (tight) {
        if (natural <= box) row.classList.remove("tight");
      } else if (natural > box + FIT_HYSTERESIS) {
        row.classList.add("tight");
      }
    },
  };
}

/** The one-shot verdict, for a caller with no generation to track. */
export function fitStrip(
  row: HTMLElement | null | undefined,
  need: (row: HTMLElement) => number = measureRowNeed,
): void {
  createStripFit(need).run(row);
}

const KID_OPTS: MutationObserverInit = {
  subtree: true,
  childList: true,
  characterData: true,
  attributes: true,
  attributeFilter: ["class", "hidden"],
};

/**
 * A ref CALLBACK rather than a ref plus an effect, because the row is
 * conditionally rendered (`stripShown`) and the observers have to follow it in
 * and out rather than be wired once against a node that may not be there.
 */
export function useFitStrip(): (row: HTMLElement | null) => void {
  const off = useRef<(() => void) | null>(null);
  return useCallback((row: HTMLElement | null) => {
    off.current?.();
    off.current = null;
    if (!row) return;
    // ONE decider per seating of the row, so the natural width it caches dies
    // with the node it was measured on.
    const fit = createStripFit();
    const run = (): void => fit.run(row);
    // A CONTENT CHANGE IS A NEW GENERATION: the cached natural width described
    // the seats as they were, and a swapped face or an arriving picker makes it
    // a lie. Re-measured on the next verdict, which is this one.
    const recheck = (): void => {
      fit.invalidate();
      fit.run(row);
    };
    const ro = typeof ResizeObserver === "function" ? new ResizeObserver(run) : null;
    ro?.observe(row);
    const kids =
      typeof MutationObserver === "function" ? new MutationObserver(recheck) : null;
    const seat = (): void => {
      kids?.disconnect();
      for (const kid of Array.from(row.children)) kids?.observe(kid, KID_OPTS);
    };
    const self =
      typeof MutationObserver === "function"
        ? new MutationObserver(() => {
            seat();
            recheck();
          })
        : null;
    // childList ONLY: the row's own attributes are what this writes.
    self?.observe(row, { childList: true });
    seat();
    run();
    off.current = () => {
      ro?.disconnect();
      kids?.disconnect();
      self?.disconnect();
    };
  }, []);
}
