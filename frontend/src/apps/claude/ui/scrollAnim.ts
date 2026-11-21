// A FAST SCROLL, NOT A JUMP AND NOT THE BROWSER'S GLIDE (Akshil 2026-11-21:
// "every transition should be scroll behaviour … under 100ms so it feels
// fast"). `scrollTo({behavior: "smooth"})` takes as long as the browser likes —
// a few hundred ms, longer over a long log — and an instant write reads as a
// cut. So the jump button drives `scrollTop` itself, eased, over a fixed time.
//
// The target is read EVERY FRAME, so a log still growing under a jump to the
// end lands on the end it has by then. Any gesture of the reader's (wheel,
// touch, a press, a key) cancels it on the spot: they have taken over.

/** How long a jump takes, in ms. */
export const JUMP_MS = 100;

/** Ease in and out (cubic): starts soft, ends soft. */
const easeInOut = (t: number) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);

/**
 * Scroll `el` to `target()` over `ms`. Instant when motion is reduced or
 * there are no frames to animate in (a test renderer). `onDone(landed)` runs
 * once: `true` at the end of the run, `false` when a gesture cancelled it.
 * Returns the cancel.
 */
export function animateScroll(
  el: HTMLElement,
  target: () => number,
  onDone?: (landed: boolean) => void,
  ms: number = JUMP_MS,
): () => void {
  const reduce =
    typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduce || typeof requestAnimationFrame === "undefined" || ms <= 0) {
    el.scrollTop = target();
    onDone?.(true);
    return () => {};
  }
  const from = el.scrollTop;
  const now = () => (typeof performance !== "undefined" ? performance.now() : Date.now());
  const start = now();
  let frame = 0;
  let over = false;
  const stopOn = ["wheel", "touchstart", "pointerdown", "keydown"] as const;
  const finish = (landed: boolean) => {
    if (over) return;
    over = true;
    if (frame && typeof cancelAnimationFrame !== "undefined") cancelAnimationFrame(frame);
    for (const t of stopOn) el.removeEventListener(t, cancel, true);
    onDone?.(landed);
  };
  const cancel = () => finish(false);
  for (const t of stopOn) el.addEventListener(t, cancel, { capture: true, passive: true });
  const step = () => {
    frame = 0;
    if (over) return;
    const t = Math.min(1, (now() - start) / ms);
    el.scrollTop = from + (target() - from) * easeInOut(t);
    if (t < 1) frame = requestAnimationFrame(step);
    else finish(true);
  };
  frame = requestAnimationFrame(step);
  return cancel;
}
