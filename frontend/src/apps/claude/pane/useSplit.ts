// THE SPLIT RATIO — view state, so it lives in a param (T:5886-5921).
//
// Everything about the geometry is a pure function here, and the hook is the
// thin React shell over it, for the reason T's own comment gives: the clamp, the
// default and the pointer→percent conversion are the parts a test can pin, and
// they are the parts a re-implementation would get subtly wrong.
import { useCallback, useEffect, useRef, useState } from "react";
import type { ParamsStore } from "../params/store";

/** T:5887 — `clampPct`. The floors are the columns' minimum useful widths (see
 *  the 800px breakpoint's derivation, T:3700-3717). */
export const SPLIT_MIN = 20;
export const SPLIT_MAX = 80;
/** T:5905 — the param's default when unset. */
export const SPLIT_DEFAULT = "70";

export function clampPct(pct: number): number {
  return Math.min(SPLIT_MAX, Math.max(SPLIT_MIN, pct));
}

/** The param → a percentage. A missing or unparseable value reads as the
 *  default: `parseFloat("")` is NaN, and `clampPct(NaN)` would be NaN, so the
 *  fallback is applied to the STRING exactly as T does (T:5905). */
export function splitPctFromParam(value: string | undefined | null): number {
  const pct = parseFloat(value || SPLIT_DEFAULT);
  return clampPct(Number.isNaN(pct) ? parseFloat(SPLIT_DEFAULT) : pct);
}

/** Pointer x → the left column's percentage (T:5913). */
export function pctFromPointer(clientX: number, innerWidth: number): number {
  return clampPct((clientX / innerWidth) * 100);
}

/**
 * The inline width the left column should carry, `undefined` meaning "write no
 * inline width at all" (T:5892-5906).
 *
 * Below the breakpoint the stylesheet owns the column's width (one view at a
 * time, no divider, nothing to be a ratio OF), and an inline width would beat it
 * — inline outranks every rule short of the CSS force flag, which this codebase
 * does not use for this (D146). So the inline declaration is CLEARED while
 * narrow and rewritten from the param on the way back out; the PARAM itself is
 * never touched, so the wide layout always returns with the user's own ratio,
 * with no reload. No pane, no ratio either: `split` then describes a layout this
 * target does not have.
 */
export function splitWidth(
  param: string | undefined,
  narrow: boolean,
  noPane: boolean,
  dragPct?: number | null,
): string | undefined {
  if (noPane || narrow) return undefined;
  if (dragPct !== null && dragPct !== undefined) return clampPct(dragPct) + "%";
  return splitPctFromParam(param) + "%";
}

export interface UseSplitOptions {
  params: ParamsStore;
  /** `NARROW_MQ.matches` (useNarrowView). */
  narrow: boolean;
  /** `enterNoPane` has run. */
  noPane: boolean;
  /** Called on every drag move. T calls `renderAnn()` here so the annotation
   *  pins — positioned in the pane's coordinates — track the drag (T:5914). PR3
   *  wires it; unset is a no-op. */
  onDragTick?: () => void;
  /** Injected for tests. */
  win?: Pick<Window, "innerWidth" | "addEventListener" | "removeEventListener">;
}

export interface SplitState {
  /** For `style={{ width }}` on the left column. */
  width: string | undefined;
  /** Stamp `.dragging` on the chat root: it paints the handle's hover colour and
   *  turns off the iframe's pointer events so the drag cannot be swallowed by
   *  the framed document (T:1254-1255). */
  dragging: boolean;
  /** `onPointerDown` for the divider. */
  onPointerDown: (ev: { clientX: number; preventDefault: () => void }) => void;
}

export function useSplit(opts: UseSplitOptions): SplitState {
  const { params, narrow, noPane, onDragTick } = opts;
  const [dragPct, setDragPct] = useState<number | null>(null);
  const [param, setParam] = useState<string | undefined>(() => params.get("split"));

  useEffect(() => params.onChange((all) => setParam(all.split)), [params]);

  // The tick callback is read through a ref so a re-created closure does not
  // have to tear down a live drag.
  const tick = useRef(onDragTick);
  tick.current = onDragTick;

  // THE LIVE DRAG'S TEARDOWN, held here rather than closed over: a drag can end
  // in three ways and only one of them is a `pointerup`. `pointercancel` — a
  // touch the browser took over, the pane navigating under the pointer — used
  // to leave both window listeners alive for good and `dragging` latched true,
  // which also left `.dragging`'s "iframe ignores the pointer" rule on. And an
  // unmount mid-drag has to take them with it.
  const release = useRef<(() => void) | null>(null);
  useEffect(() => () => release.current?.(), []);

  const onPointerDown = useCallback(
    (ev: { clientX: number; preventDefault: () => void }) => {
      if (noPane || narrow) return;
      ev.preventDefault();
      const win = opts.win ?? window;
      release.current?.(); // a second press without a release is still one drag
      setDragPct(pctFromPointer(ev.clientX, win.innerWidth));
      const move = (e: Event) => {
        const pct = pctFromPointer((e as PointerEvent).clientX, win.innerWidth);
        setDragPct(pct);
        tick.current?.();
      };
      const detach = () => {
        win.removeEventListener("pointermove", move);
        win.removeEventListener("pointerup", up);
        win.removeEventListener("pointercancel", cancel);
        release.current = null;
      };
      const up = (e: Event) => {
        detach();
        const pct = pctFromPointer((e as PointerEvent).clientX, win.innerWidth);
        // ONE param write, on release, as `toFixed(1)` (T:5919). `history:
        // "replace"` because a drag is not a navigation: the once-per-visit push
        // belongs to a real user choice, not to sizing a column (D268) — a
        // DIVERGENCE from T:5919's bare set, recorded in design.md §2.
        params.set({ split: pct.toFixed(1) }, { history: "replace" });
        setDragPct(null);
      };
      // A cancelled drag is a drag that did not happen: the column goes back to
      // the param it had, and nothing is written.
      const cancel = () => {
        detach();
        setDragPct(null);
      };
      release.current = cancel;
      win.addEventListener("pointermove", move);
      win.addEventListener("pointerup", up);
      win.addEventListener("pointercancel", cancel);
    },
    [narrow, noPane, params, opts.win],
  );

  return {
    width: splitWidth(param, narrow, noPane, dragPct),
    dragging: dragPct !== null,
    onPointerDown,
  };
}
