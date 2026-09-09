// THE TYPER'S CLOCK, and the whole of what makes it hidden-tab-safe.
//
// `requestAnimationFrame` does not fire while the document is hidden, and the
// typer's drain is a STATE, not a fire-and-forget: while it lasts the typer
// stays pointed at the turn that just ended and `retarget` stands down. So a
// frame that never arrives does not merely stall the animation — it leaves
// `finish()` pending forever, latches `draining`, and every later turn in that
// mount stops being typed at all.
//
// Deciding per `schedule()` call (hidden → `setTimeout`) closes only half of
// that: a frame handed to rAF while the tab was VISIBLE is already in a queue
// that stops the instant the tab hides. `rescueHidden` is the other half —
// on `visibilitychange` the frames still in flight are moved onto timers.
//
// Handles are this module's OWN sequence, because an rAF id and a timeout id
// are separate sequences and can collide.

export interface FrameClock {
  schedule(cb: () => void): number;
  cancel(handle: number): void;
  /** The document just hid: re-arm every in-flight rAF frame onto a timer. */
  rescueHidden(): void;
  /** Test-only view of what is still in flight. */
  pending(): Array<{ handle: number; raf: boolean }>;
}

interface Live {
  raf: boolean;
  id: number;
  run: () => void;
}

/** Injectable for tests; the defaults are the real browser's. */
export interface FrameClockEnv {
  hidden(): boolean;
  raf: ((cb: () => void) => number) | null;
  cancelRaf(id: number): void;
  setTimer(cb: () => void, ms: number): number;
  clearTimer(id: number): void;
}

function browserFrameEnv(): FrameClockEnv {
  return {
    hidden: () => typeof document !== "undefined" && document.hidden,
    raf: typeof requestAnimationFrame === "function" ? (cb) => requestAnimationFrame(cb) : null,
    cancelRaf: (id) => cancelAnimationFrame(id),
    setTimer: (cb, ms) => setTimeout(cb, ms) as unknown as number,
    clearTimer: (id) => clearTimeout(id),
  };
}

/** ~60 fps, the same interval the old inline fallback used. */
export const FRAME_TIMER_MS = 16;

export function createFrameClock(env: FrameClockEnv = browserFrameEnv()): FrameClock {
  const live = new Map<number, Live>();
  let nextHandle = 0;
  const arm = (handle: number, run: () => void) => {
    if (!env.hidden() && env.raf) live.set(handle, { raf: true, id: env.raf(run), run });
    else live.set(handle, { raf: false, id: env.setTimer(run, FRAME_TIMER_MS), run });
  };
  return {
    schedule(cb) {
      const handle = ++nextHandle;
      const run = () => {
        live.delete(handle);
        cb();
      };
      arm(handle, run);
      return handle;
    },
    cancel(handle) {
      const entry = live.get(handle);
      if (!entry) return;
      live.delete(handle);
      if (entry.raf) env.cancelRaf(entry.id);
      else env.clearTimer(entry.id);
    },
    rescueHidden() {
      // Snapshot first: `arm` writes back into the same map.
      for (const [handle, entry] of [...live]) {
        if (!entry.raf) continue;
        env.cancelRaf(entry.id);
        arm(handle, entry.run);
      }
    },
    pending: () => [...live].map(([handle, e]) => ({ handle, raf: e.raf })),
  };
}
