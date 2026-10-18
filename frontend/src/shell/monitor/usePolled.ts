// Run `tick` now and then every `ms` while the document is visible, for the
// dashboard tabs. `tick` is read through a ref, so a new closure each render
// never restarts the timer; a rejection is `onError`'s, never an unhandled
// one. `key` restarts the loop (and its immediate first tick) when it changes.
import { useEffect, useRef } from "react";

const visible = () => typeof document === "undefined" || document.visibilityState !== "hidden";

export function usePolled(
  tick: () => Promise<void> | void,
  ms: number,
  key: unknown = null,
  onError?: (e: Error) => void,
) {
  const fn = useRef(tick);
  fn.current = tick;
  const err = useRef(onError);
  err.current = onError;
  useEffect(() => {
    let live = true;
    let busy = false;
    const run = async () => {
      if (!live || busy || !visible()) return;
      busy = true;
      try {
        await fn.current();
      } catch (e) {
        if (live) err.current?.(e as Error);
      } finally {
        busy = false;
      }
    };
    void run();
    const id = setInterval(run, ms);
    return () => {
      live = false;
      clearInterval(id);
    };
  }, [ms, key]);
}
