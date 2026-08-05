// FLIP row animation for the listing table, plus the "which rows are new" diff
// the dir-watch cue needs.
//
// FLIP = First, Last, Invert, Play: measure where each row sits BEFORE the
// commit, let React reorder the DOM, then translate every moved row back to
// where it was and transition that transform away. The rows glide to their new
// slots instead of teleporting, and because it is one CSS transition per row it
// inherits the global prefers-reduced-motion kill-switch for free.
//
// Rows are matched by a stable key (the row's path), never by index — a re-sort
// or a refetch replaces every object in the list, so index identity is
// meaningless here. The measure/diff halves are pure and unit-tested
// (flip.test.ts); only useFlip touches the DOM.
import { useLayoutEffect, useRef, type RefObject } from "react";

// The attribute useFlip identifies rows by. Rows opt in by carrying it.
export const FLIP_KEY_ATTR = "data-flip-key";

// Must match the .flip-animating transition in shell.css (--dur-slow): the CSS
// owns the animation, this only decides when the inline transform is cleaned up.
export const FLIP_MS = 200;

// key -> offsetTop, in the scroll container's coordinate space.
export type RowOffsets = Map<string, number>;

// How far each row has to be shifted to APPEAR where it was, given the offsets
// before and after a commit. Only keys present in both snapshots that actually
// moved: a row that just appeared has no previous position to come from (it
// fades/highlights instead), and a row that stayed put must not get a transform
// at all — an identity transform still creates a compositing layer per row,
// which on a 5000-row listing is exactly the cost this is meant to avoid.
export function flipDeltas(before: RowOffsets, after: RowOffsets): Map<string, number> {
  const deltas = new Map<string, number>();
  for (const [key, top] of after) {
    const was = before.get(key);
    if (was === undefined || was === top) continue;
    deltas.set(key, was - top);
  }
  return deltas;
}

// Keys in `next` that weren't in `prev`. A null `prev` means there is no
// previous list to compare against (the folder just opened): nothing counts as
// new then, because "every row is new" is both noise and a statement about the
// viewer, not about the folder.
export function appearedKeys(
  prev: readonly string[] | null,
  next: readonly string[],
): Set<string> {
  const fresh = new Set<string>();
  if (prev === null) return fresh;
  const had = new Set(prev);
  for (const key of next) if (!had.has(key)) fresh.add(key);
  return fresh;
}

function measure(root: HTMLElement): RowOffsets {
  const offsets: RowOffsets = new Map();
  for (const el of root.querySelectorAll<HTMLElement>(`[${FLIP_KEY_ATTR}]`)) {
    const key = el.getAttribute(FLIP_KEY_ATTR);
    if (key !== null) offsets.set(key, el.offsetTop);
  }
  return offsets;
}

// Animate keyed rows inside `containerRef` to their new positions whenever
// `signal` changes. `signal` is whatever the caller wants to treat as "a commit
// that may have reordered rows" — a sort key, a refresh generation, the rendered
// row list. The measurement is taken on every run and kept for the next one, so
// the hook needs no explicit before/after bracketing at the call sites.
//
// Layout effect: the invert has to be applied in the same frame as the commit,
// or the browser paints the rows in their new places first and there is nothing
// left to animate from.
export function useFlip(
  containerRef: RefObject<HTMLElement | null>,
  signal: unknown,
  enabled = true,
): void {
  const previous = useRef<RowOffsets>(new Map());
  const cleanupTimer = useRef<number | null>(null);
  useLayoutEffect(() => {
    const root = containerRef.current;
    if (!root) return;
    const now = measure(root);
    const before = previous.current;
    previous.current = now;
    if (!enabled) return;
    const deltas = flipDeltas(before, now);
    if (deltas.size === 0) return;

    const moved: HTMLElement[] = [];
    for (const el of root.querySelectorAll<HTMLElement>(`[${FLIP_KEY_ATTR}]`)) {
      const dy = deltas.get(el.getAttribute(FLIP_KEY_ATTR) ?? "");
      if (dy === undefined) continue;
      // No transition class yet, so this jump is instant — which is the point:
      // the row is put back where the user last saw it.
      el.style.transform = `translateY(${dy}px)`;
      moved.push(el);
    }

    const play = requestAnimationFrame(() => {
      for (const el of moved) {
        el.classList.add("flip-animating");
        el.style.transform = "";
      }
      // Drop the transition once it's over, so an unrelated later transform
      // (or a fresh FLIP) isn't animated by a leftover rule.
      if (cleanupTimer.current !== null) window.clearTimeout(cleanupTimer.current);
      cleanupTimer.current = window.setTimeout(() => {
        cleanupTimer.current = null;
        for (const el of moved) el.classList.remove("flip-animating");
      }, FLIP_MS);
    });

    return () => {
      cancelAnimationFrame(play);
      // Interrupted mid-animation (another reorder, or unmount): leave no row
      // holding an inline transform or a stray transition class.
      for (const el of moved) {
        el.style.transform = "";
        el.classList.remove("flip-animating");
      }
      if (cleanupTimer.current !== null) {
        window.clearTimeout(cleanupTimer.current);
        cleanupTimer.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signal, enabled]);
}
