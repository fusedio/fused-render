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
//
// A SHELL TAB THAT IS NOT THE ACTIVE ONE is hidden too (shell/TabHost.tsx).
// Its iframe is display:none inside a window that is on screen, so its
// document still says "visible" — the host says otherwise through
// `window._fusedTabHidden`, and announces each change with a
// `fused:tab-visibility` event on the tab's window. Either signal parks; the
// first moment BOTH say visible runs the catch-up, once.
//
// A FRAME INSIDE A TAB is exactly as hidden as the tab (Home's preview cards,
// the app page's running app, an explorer pane): the flag lives on the TAB's
// window, so the gate climbs to it — the nearest same-origin ancestor-or-self
// whose parent is the host — and listens for the event there.
export type HiddenGated = (() => void) & { cancel: () => void };

export const TAB_VISIBILITY_EVENT = "fused:tab-visibility";

// The shell tab this document sits in (itself, or a same-origin ancestor), or
// null outside shell tabs. A cross-origin ancestor ends the climb.
function tabWindow(): Window | null {
  if (typeof window === "undefined") return null;
  try {
    let w: Window = window;
    while (w.parent && w.parent !== w) {
      if (w.parent._fusedTabs) return w;
      w = w.parent;
    }
  } catch {
    /* cross-origin ancestor: no tab above us that we could read */
  }
  return null;
}

function isHidden(tab: Window | null): boolean {
  // No document (tests, workers) or a shim without visibilityState: visible.
  if (typeof document === "undefined") return false;
  if (document.visibilityState === "hidden") return true;
  try {
    return tab?._fusedTabHidden === true;
  } catch {
    return false;
  }
}

export function pauseWhileHidden(fn: () => void): HiddenGated {
  let parked = false;
  // Where the tab event is heard — resolved per park, since a gate can be
  // created before its document has settled into a frame.
  let heardOn: Window | null = null;
  const detach = () => {
    document.removeEventListener("visibilitychange", onVisibility);
    try {
      heardOn?.removeEventListener(TAB_VISIBILITY_EVENT, onVisibility);
    } catch {
      /* the tab went away */
    }
    heardOn = null;
  };
  const onVisibility = () => {
    if (isHidden(tabWindow())) return;
    detach();
    if (!parked) return;
    parked = false;
    fn();
  };
  const gated = (() => {
    const tab = tabWindow();
    if (!isHidden(tab)) {
      fn();
      return;
    }
    if (parked) return;
    parked = true;
    document.addEventListener("visibilitychange", onVisibility);
    heardOn = tab ?? window;
    try {
      heardOn.addEventListener(TAB_VISIBILITY_EVENT, onVisibility);
    } catch {
      heardOn = null;
    }
  }) as HiddenGated;
  gated.cancel = () => {
    parked = false;
    if (typeof document !== "undefined") detach();
  };
  return gated;
}
