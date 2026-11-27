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
// A WORKSPACE FRAME THAT IS NOT SHOWN is hidden too (D1514, shell/workspace).
// Its iframe is display:none inside a window that is on screen, so its document
// still says "visible" — the host says otherwise through `_fusedTabHidden` on
// the frame's window, and announces each change with `fused:tab-visibility`
// there. Either signal parks; the first moment both say visible catches up.
// A frame INSIDE such a frame (Home's preview cards) is exactly as hidden: the
// gate climbs to the host's direct child and listens there.
export type HiddenGated = (() => void) & { cancel: () => void };

export const TAB_VISIBILITY_EVENT = "fused:tab-visibility";

// The workspace frame this document sits in (itself, or a same-origin
// ancestor whose parent is the host), or null outside one.
function tabWindow(): Window | null {
  if (typeof window === "undefined") return null;
  try {
    let w: Window = window;
    while (w.parent && w.parent !== w) {
      if (w.parent._fusedWorkspace) return w;
      w = w.parent;
    }
  } catch {
    /* cross-origin ancestor: no host above us that we could read */
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
  // Where the frame event is heard — resolved per park.
  let heardOn: Window | null = null;
  const detach = () => {
    document.removeEventListener("visibilitychange", onVisibility);
    try {
      heardOn?.removeEventListener(TAB_VISIBILITY_EVENT, onVisibility);
    } catch {
      /* the frame went away */
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
    if (tab) {
      try {
        tab.addEventListener(TAB_VISIBILITY_EVENT, onVisibility);
        heardOn = tab;
      } catch {
        heardOn = null;
      }
    }
  }) as HiddenGated;
  gated.cancel = () => {
    parked = false;
    if (typeof document !== "undefined") detach();
  };
  return gated;
}
