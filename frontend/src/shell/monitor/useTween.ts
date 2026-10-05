// A number that eases to each new value instead of jumping — the meters'
// figures. 200 ms, ease-out, on requestAnimationFrame; a timer snaps it to
// the target regardless, so a document that never paints a frame (a hidden
// tab, a pane with no rAF) still ends on the right number. Reduced motion
// skips the tween entirely.
import { useEffect, useRef, useState } from "react";

export const TWEEN_MS = 200;

const reduced = () =>
  typeof window !== "undefined" &&
  typeof window.matchMedia === "function" &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/** ease-out cubic */
const ease = (k: number) => 1 - (1 - k) ** 3;

export function useTween(target: number | null, ms = TWEEN_MS): number | null {
  const [shown, setShown] = useState(target);
  const from = useRef(target);
  useEffect(() => {
    const start = from.current;
    if (target === null || start === null || start === target || reduced() ||
        typeof requestAnimationFrame !== "function") {
      from.current = target;
      setShown(target);
      return;
    }
    const t0 = performance.now();
    let raf = 0;
    const step = (now: number) => {
      const k = Math.min(1, (now - t0) / ms);
      const v = start + (target - start) * ease(k);
      from.current = v;
      setShown(v);
      if (k < 1) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    const snap = setTimeout(() => {
      cancelAnimationFrame(raf);
      from.current = target;
      setShown(target);
    }, ms + 50);
    return () => {
      cancelAnimationFrame(raf);
      clearTimeout(snap);
    };
  }, [target, ms]);
  return shown;
}
