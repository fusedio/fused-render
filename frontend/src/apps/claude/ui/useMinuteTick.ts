// ONE CLOCK FOR EVERY RELATIVE STAMP ON THE PAGE.
//
// `3m ago` is a sentence about now, and a string computed when a row settled
// keeps saying `just now` for as long as the transcript stays mounted (Bugbot,
// PR #1430 — twice: a rewrite on `pointerover` lost too, because the hint
// panel reads the attribute in the capture phase, before any React handler).
// So the stamps re-render on a shared tick instead: one interval for the whole
// document, started by the first subscriber and stopped by the last, at half
// the coarsest unit the words use (a minute), so a stamp is never more than
// thirty seconds behind and no stamp owns a timer of its own.
import { useEffect, useState } from "react";

export const MINUTE_TICK_MS = 30_000;

const listeners = new Set<() => void>();
let timer: ReturnType<typeof setInterval> | null = null;

function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  if (timer === null) {
    timer = setInterval(() => {
      for (const l of listeners) l();
    }, MINUTE_TICK_MS);
  }
  return () => {
    listeners.delete(fn);
    if (!listeners.size && timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };
}

/** A counter that advances every `MINUTE_TICK_MS` while the component is
 *  mounted — read it (or ignore it) to re-render relative words on time. */
export function useMinuteTick(): number {
  const [tick, setTick] = useState(0);
  useEffect(() => subscribe(() => setTick((n) => n + 1)), []);
  return tick;
}

/** Test seam: how many components are listening right now. */
export function minuteTickListeners(): number {
  return listeners.size;
}
