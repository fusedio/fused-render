// Page-side half of the black-window diagnostics (the window half is
// `fused_render/chromium/mac.py`, `_ChromiumDelegate._log_visibility`).
//
// A Chromium (CEF) window has been seen to stop painting after a Space
// switch or the relaunch handoff — black until ⌘R — and it does not reproduce
// in isolation. What the page can tell the server is whether, at that moment,
// it believes it is VISIBLE and whether its requestAnimationFrame loop is
// RUNNING: a black window with `visible` + rAF alive means frames are made
// but never displayed (compositor side); `hidden` means the browser still
// thinks the window is occluded (visibility side); rAF stalled while
// `visible` means the renderer is frozen. Each report is one POST to
// `/api/health/visibility`, logged beside the window-side lines.
//
// Cost: one rAF callback per frame (a subtraction) and a 1 s interval; a
// report only on `visibilitychange`, focus/blur, pageshow, and a stall edge.
import { IS_EMBED, IS_SNAPSHOT } from "@platform/lib/router";

/** A rAF gap this long while the document says `visible` is a stall: the
 *  renderer is not producing frames even though the browser says it should. */
export const RAF_STALL_MS = 2_000;
/** The stall is over once frames are this fresh again. */
export const RAF_RESUMED_MS = 500;

export type StallEdge = "raf-stalled" | "raf-resumed" | null;

/** Pure: the edge to report, if any, given the previous stalled flag, the
 *  document's visibility and how long ago the last rAF callback ran. */
export function stallTransition(stalled: boolean, visible: boolean, sinceRafMs: number): StallEdge {
  if (!stalled && visible && sinceRafMs > RAF_STALL_MS) return "raf-stalled";
  if (stalled && sinceRafMs < RAF_RESUMED_MS) return "raf-resumed";
  return null;
}

export function installVisibilityDiag(page: () => string): void {
  if (IS_EMBED || IS_SNAPSHOT) return; // the top-level shell document only
  if (typeof document === "undefined" || typeof requestAnimationFrame !== "function") return;
  let lastRaf = performance.now();
  let maxGap = 0;
  let stalled = false;
  const tick = (t: number) => {
    const gap = t - lastRaf;
    lastRaf = t;
    if (gap > maxGap) maxGap = gap;
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);

  const send = (ev: string) => {
    const body = {
      ev,
      page: page(),
      state: document.visibilityState,
      hasFocus: document.hasFocus(),
      rafGap: Math.round(maxGap),
      sinceRaf: Math.round(performance.now() - lastRaf),
      t: Date.now() / 1000,
    };
    maxGap = 0;
    fetch("/api/health/visibility", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Fused": "1" },
      body: JSON.stringify(body),
      keepalive: true,
    }).catch(() => {
      /* diagnostics: a lost report is nothing to surface */
    });
  };

  document.addEventListener("visibilitychange", () => send("visibilitychange"));
  window.addEventListener("focus", () => send("focus"));
  window.addEventListener("blur", () => send("blur"));
  window.addEventListener("pageshow", () => send("pageshow"));
  setInterval(() => {
    const edge = stallTransition(stalled, document.visibilityState === "visible", performance.now() - lastRaf);
    if (edge === null) return;
    stalled = edge === "raf-stalled";
    send(edge);
  }, 1_000);
}
