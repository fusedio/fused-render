// The typewriter, as a pure scheduler. `T`'s `makeTyper` (T:15063-15142) owns
// an element: it writes `renderMd(target.slice(0, shown))` into a body node on
// each frame. Here the cadence is separated from the paint — `onFrame` hands the
// UI the slice and the UI renders it — so the rule can be tested without a DOM
// and the React side keeps ownership of the markdown funnel.
//
// CADENCE CONSTANTS, all with their T line:
//   * CHARS_PER_TICK_DIVISOR = 36 — `step = max(1, ceil(remaining / 36))`, so a
//     target of any length drains in ~36 frames (T:15084).
//   * PAINT_MIN_MS = 40 — renderMd re-parses the WHOLE reply so far, so a paint
//     per frame is O(n) per frame and O(n²) across a long reply. Capping at
//     ~25fps is not perceptible for text arriving this way, and the frame that
//     COMPLETES the drain always paints, so the settled reply is never a frame
//     behind (T:15086-15098).
//   * The clamp: a target rewritten SHORTER (the authoritative final text) must
//     redraw once immediately, or the frozen frame keeps stale text
//     (T:15075-15080).
//   * `retarget(el|null)` resets `target`/`shown` and cancels the pending frame:
//     the counters measure progress through the NEW element's text, and the
//     pending frame would draw the old text into the new element (T:15130-15141).
//     Callers own the old element and must render it whole first — which is
//     exactly what `renderSegments` does before every retarget (T:15668-15676).
//   * A PARKED typer (`target === null`) draws nothing, not even its cursor: the
//     reply's element exists before its first text segment does, because a turn
//     can open with a tool call (T:15057-15062).
//   * `finish(text)` waits for the drain, THEN resolves — the caller runs its
//     one highlight/copy pass over the finished transcript afterwards
//     (T:15116-15127, called at T:16336).

/** T:15084. */
export const CHARS_PER_TICK_DIVISOR = 36;
/** T:15086-15095 — ~25fps. */
export const PAINT_MIN_MS = 40;

/** What the UI paints. `cursor` is T's `span.cursor` "▋" (T:15063-15066): shown
 *  while the typer is attached and not yet finished-and-drained. */
export interface TyperFrame {
  /** The row the typer is attached to, `null` while parked. */
  key: string | null;
  /** `target.slice(0, shown)` — the text to render this frame. */
  text: string;
  /** Whether the caret belongs on screen. */
  cursor: boolean;
  /** True on the frame that completed the drain. */
  drained: boolean;
}

export interface TyperDeps {
  /** Called on every PAINT (not every scheduled tick). */
  onFrame(frame: TyperFrame): void;
  /** ms clock — `Date.now` in the browser (T:15096). */
  now(): number;
  /** A frame scheduler: `requestAnimationFrame` in the browser. Must return a
   *  handle `cancel` understands. */
  schedule(cb: () => void): number;
  cancel(handle: number): void;
}

export interface Typer {
  /** T:15113 — grow the target. No-op while parked. */
  update(text: string): void;
  /** T:15116 — the authoritative final text; resolves once drained. */
  finish(text: string): Promise<void>;
  /** T:15130 — point at a different row, or at nothing. Also re-arms the caret:
   *  T throws a finished typer away and builds the next turn's, and one
   *  long-lived typer has to be able to do the same thing. */
  retarget(key: string | null): void;
  /** T:15142 — drop the pending frame and the caret. */
  abort(): void;
  /** Test/debug: how much of the target has been handed to `onFrame`. */
  shown(): number;
}

/** T:15063 `makeTyper`. `key` is the row the typer starts attached to (`null`
 *  builds it PARKED — what a segment transcript needs). */
export function createTyper(deps: TyperDeps, key: string | null = null): Typer {
  let attached = key;
  let target = "";
  let shown = 0;
  let finished = false;
  let raf: number | null = null;
  let painted = 0; // last paint, ms — see the throttle in tick()
  const waiters: (() => void)[] = [];

  const paint = (drained: boolean) => {
    deps.onFrame({ key: attached, text: target.slice(0, shown), cursor: !(finished && drained), drained });
  };

  const settle = () => {
    // finish()'s promise resolves only once no frame is pending — T:15119-15125
    // spins on rAF for exactly this, so the caller's attachCodeCopy pass runs
    // over a fully drawn reply.
    if (raf !== null) return;
    const pending = waiters.splice(0, waiters.length);
    for (const w of pending) w();
  };

  function tick() {
    raf = null;
    if (attached === null) {
      // Parked mid-frame (retarget); nothing to draw into. T:15070.
      settle();
      return;
    }
    if (shown > target.length) {
      // The clamp — T:15075-15080.
      shown = target.length;
      paint(true);
      painted = deps.now();
    }
    const remaining = target.length - shown;
    if (remaining > 0) {
      const step = Math.max(1, Math.ceil(remaining / CHARS_PER_TICK_DIVISOR));
      shown = Math.min(target.length, shown + step);
      const now = deps.now();
      if (now - painted >= PAINT_MIN_MS || shown === target.length) {
        painted = now;
        paint(shown === target.length);
      }
    }
    if (shown < target.length) raf = deps.schedule(tick);
    else {
      if (finished) paint(true);
      settle();
    }
  }

  const kick = () => {
    if (raf === null) raf = deps.schedule(tick);
  };

  return {
    update(text) {
      if (attached === null) return; // parked: the tail of this reply is not prose
      target = text;
      kick();
    },
    finish(text) {
      finished = true;
      if (attached === null) {
        // T:15118 — nothing to drain; retire the caret and resolve at once.
        deps.onFrame({ key: null, text: "", cursor: false, drained: true });
        // AND SETTLE THE EARLIER WAITERS. A `finish` whose frame was cancelled
        // by a `retarget(null)` has a resolve function sitting in `waiters`
        // that nothing else will ever call — and its caller's `draining` flag
        // stays set, which stands the typer down for every turn after
        // (ClaudeChat's drain effect).
        settle();
        return Promise.resolve();
      }
      target = text;
      kick();
      return new Promise<void>((resolve) => {
        waiters.push(resolve);
        settle();
      });
    },
    retarget(next) {
      if (next === attached) return;
      if (raf !== null) {
        deps.cancel(raf);
        raf = null;
      }
      attached = next;
      target = "";
      shown = 0;
      // `finished` is CLEARED here, which is the one place this differs from T —
      // and it differs in order to match it. T builds a typer per turn
      // (`makeTyper` inside pollLoop), so a finished typer is simply thrown away
      // and the next turn gets a fresh caret. React keeps ONE typer for the life
      // of the mount and moves it, so without this reset the caret retired on
      // the first turn that ever ended and never came back for any turn after —
      // and, inside a turn, the tail moving past a drained segment would have
      // put the reply into the "finished" branch early (T:15108).
      finished = false;
      paint(true);
      settle();
    },
    abort() {
      if (raf !== null) {
        deps.cancel(raf);
        raf = null;
      }
      finished = true;
      attached = null;
      deps.onFrame({ key: null, text: "", cursor: false, drained: true });
      settle();
    },
    shown() {
      return shown;
    },
  };
}
