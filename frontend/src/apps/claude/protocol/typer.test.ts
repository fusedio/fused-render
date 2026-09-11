// The typer's cadence, driven by a manual clock and a manual frame queue — no
// rAF, no DOM. Pins the four numbers T:15057-15142 depends on.
import { describe, expect, test } from "bun:test";

import { CHARS_PER_TICK_DIVISOR, createTyper, PAINT_MIN_MS, type TyperFrame } from "./typer";

/** A frame pump: `flush()` runs whatever is queued, `tick(ms)` advances time. */
function harness(startAt = 0) {
  let clock = startAt;
  let next = 1;
  const queue = new Map<number, () => void>();
  const frames: TyperFrame[] = [];
  const deps = {
    onFrame: (f: TyperFrame) => frames.push({ ...f }),
    now: () => clock,
    schedule: (cb: () => void) => {
      const h = next++;
      queue.set(h, cb);
      return h;
    },
    cancel: (h: number) => {
      queue.delete(h);
    },
  };
  return {
    deps,
    frames,
    advance: (ms: number) => {
      clock += ms;
    },
    /** Run every queued callback once (one animation frame). */
    frame() {
      const pending = [...queue.entries()];
      queue.clear();
      for (const [, cb] of pending) cb();
      return pending.length;
    },
    /** Run frames until the queue is empty (with `ms` between them). */
    drain(ms = PAINT_MIN_MS) {
      for (let i = 0; i < 500 && queue.size; i++) {
        clock += ms;
        this.frame();
      }
      return queue.size;
    },
    pending: () => queue.size,
  };
}

describe("cadence", () => {
  test("step is ceil(remaining / 36), at least 1 (T:15084)", () => {
    const h = harness();
    const t = createTyper(h.deps, "row");
    t.update("x".repeat(360));
    h.frame();
    expect(t.shown()).toBe(360 / CHARS_PER_TICK_DIVISOR);
    // remaining 350 ⇒ ceil(350/36) = 10
    h.advance(PAINT_MIN_MS);
    h.frame();
    expect(t.shown()).toBe(20);
  });

  test("a one-character target still advances", () => {
    const h = harness();
    const t = createTyper(h.deps, "row");
    t.update("x");
    h.frame();
    expect(t.shown()).toBe(1);
    expect(h.frames[h.frames.length - 1].text).toBe("x");
  });

  test("the drain is GEOMETRIC, not 36 frames flat", () => {
    // T:15084's comment says "drain in ~36 frames", and that is true of the
    // FIRST frame's share (1/36 of the target) rather than of the whole drain:
    // `remaining` shrinks by a 36th each frame, so a long reply takes ~36·ln(n)
    // frames. Pinned as measured, so a change to the divisor is visible here
    // rather than in a comment nobody re-derives.
    const h = harness();
    const t = createTyper(h.deps, "row");
    t.update("y".repeat(5000));
    let frames = 0;
    while (h.pending() && frames < 600) {
      h.advance(PAINT_MIN_MS);
      h.frame();
      frames++;
    }
    expect(t.shown()).toBe(5000);
    expect(frames).toBeGreaterThan(CHARS_PER_TICK_DIVISOR);
    expect(frames).toBeLessThan(300);
  });
});

describe("paint throttle (T:15086-15098)", () => {
  test("frames inside 40 ms do not paint, but the one that completes always does", () => {
    const h = harness();
    const t = createTyper(h.deps, "row");
    t.update("z".repeat(72));
    h.frame(); // paints (painted was 0, now - painted >= 40 with clock 0? no)
    const painted = h.frames.length;
    // Advance the counters without letting the clock move: no paint.
    h.frame();
    h.frame();
    expect(h.frames.length).toBe(painted);
    // Now let the clock pass the throttle: a paint again.
    h.advance(PAINT_MIN_MS);
    h.frame();
    expect(h.frames.length).toBe(painted + 1);
    // …and the drain's final frame paints regardless of the clock.
    h.drain(0);
    expect(t.shown()).toBe(72);
    expect(h.frames[h.frames.length - 1].drained).toBe(true);
  });
});

describe("the shorter-target clamp (T:15075)", () => {
  test("an authoritative final text shorter than what is shown redraws at once", async () => {
    const h = harness();
    const t = createTyper(h.deps, "row");
    t.update("a".repeat(200));
    h.drain();
    expect(t.shown()).toBe(200);
    const done = t.finish("short");
    h.drain();
    await done;
    expect(t.shown()).toBe(5);
    expect(h.frames[h.frames.length - 1].text).toBe("short");
  });
});

describe("finish (T:15116)", () => {
  test("resolves only once the drain is complete", async () => {
    const h = harness();
    const t = createTyper(h.deps, "row");
    let resolved = false;
    void t.finish("hello there, this is the settled reply").then(() => {
      resolved = true;
    });
    await Promise.resolve();
    expect(resolved).toBe(false);
    h.drain();
    await Promise.resolve();
    await Promise.resolve();
    expect(resolved).toBe(true);
    expect(t.shown()).toBe("hello there, this is the settled reply".length);
  });

  test("a PARKED typer retires its caret and resolves at once (T:15118)", async () => {
    const h = harness();
    const t = createTyper(h.deps, null);
    await t.finish("");
    expect(h.frames[h.frames.length - 1]).toEqual({ key: null, text: "", cursor: false, drained: true });
  });

  test("the caret goes on the frame that finishes the drain", async () => {
    const h = harness();
    const t = createTyper(h.deps, "row");
    t.update("abc");
    h.drain();
    expect(h.frames[h.frames.length - 1].cursor).toBe(true);
    const done = t.finish("abc");
    h.drain();
    await done;
    expect(h.frames[h.frames.length - 1].cursor).toBe(false);
  });
});

describe("parked / retarget / abort", () => {
  test("a parked typer draws nothing at all (T:15057)", () => {
    const h = harness();
    const t = createTyper(h.deps, null);
    t.update("ignored");
    expect(h.frames.length).toBe(0);
    expect(h.pending()).toBe(0);
    expect(t.shown()).toBe(0);
  });

  test("retarget resets the counters and cancels the pending frame (T:15130)", () => {
    const h = harness();
    const t = createTyper(h.deps, "a");
    t.update("aaaaaaaaaaaaaaaaaaaaaa");
    h.frame();
    expect(t.shown()).toBeGreaterThan(0);
    t.retarget("b");
    expect(t.shown()).toBe(0);
    expect(h.pending()).toBe(0);
    expect(h.frames[h.frames.length - 1].key).toBe("b");
    // The new row starts from nothing, not from the old row's progress.
    t.update("bb");
    h.drain();
    expect(h.frames[h.frames.length - 1]).toMatchObject({ key: "b", text: "bb" });
  });

  test("retarget to the same row is a no-op", () => {
    const h = harness();
    const t = createTyper(h.deps, "a");
    t.update("aaaa");
    h.frame();
    const before = h.frames.length;
    t.retarget("a");
    expect(h.frames.length).toBe(before);
  });

  test("abort drops the frame and the caret (T:15142)", () => {
    const h = harness();
    const t = createTyper(h.deps, "a");
    t.update("a".repeat(500));
    h.frame();
    t.abort();
    expect(h.pending()).toBe(0);
    expect(h.frames[h.frames.length - 1]).toEqual({ key: null, text: "", cursor: false, drained: true });
  });
});
