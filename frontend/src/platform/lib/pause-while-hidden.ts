// Gate a poll tick on the window being visible. Every native window (and every
// tab) shares WebKit's 6-connection HTTP/1.1 pool per host:port — measured
// 2026-10-08 — so a status poll ticking in a window nobody is looking at queues
// the VISIBLE window's real requests behind it. Wrap the tick:
//
//   setInterval(pauseWhileHidden(tick), MS)            // interval polls
//   timer = setTimeout(gated, MS)                      // timeout chains
//
// While the document is hidden a call does nothing but PARK; the first
// visibilitychange back to visible runs `fn` exactly once (the catch-up tick),
// however many ticks were skipped. A visible call runs `fn` straight through.
// `cancel()` drops a parked catch-up (call it on unmount/stop).
//
// Only for polls whose answer is for the eyes of THIS window. A poll that
// delivers something a person must hear while away (a narrator, an OS
// notification) must not be wrapped — see scheduleEvents.ts / bots store.
export type HiddenGated = (() => void) & { cancel: () => void };

export function pauseWhileHidden(fn: () => void): HiddenGated {
  let parked = false;
  const onVisibility = () => {
    if (document.visibilityState === "hidden") return;
    document.removeEventListener("visibilitychange", onVisibility);
    if (!parked) return;
    parked = false;
    fn();
  };
  const gated = (() => {
    // No document (tests, workers) or a shim without visibilityState: run.
    if (typeof document === "undefined" || document.visibilityState !== "hidden") {
      fn();
      return;
    }
    if (parked) return;
    parked = true;
    document.addEventListener("visibilitychange", onVisibility);
  }) as HiddenGated;
  gated.cancel = () => {
    parked = false;
    if (typeof document !== "undefined") document.removeEventListener("visibilitychange", onVisibility);
  };
  return gated;
}
