// A frame that was in flight when the tab hid must still be delivered: the
// typer's drain is a state, and a frame that never arrives leaves `finish()`
// pending, latches `draining`, and stops every later turn being typed.
import { expect, test } from "bun:test";
import { createFrameClock, type FrameClockEnv } from "./frameClock";

function fake() {
  let hidden = false;
  const rafs = new Map<number, () => void>();
  const timers = new Map<number, () => void>();
  let next = 0;
  const env: FrameClockEnv = {
    hidden: () => hidden,
    raf: (cb) => {
      const id = ++next;
      rafs.set(id, cb);
      return id;
    },
    cancelRaf: (id) => void rafs.delete(id),
    setTimer: (cb) => {
      const id = ++next;
      timers.set(id, cb);
      return id;
    },
    clearTimer: (id) => void timers.delete(id),
  };
  return {
    env,
    hide: () => {
      hidden = true;
    },
    /** What a hidden tab does to the rAF queue: nothing ever runs. */
    frames: () => rafs.size,
    runTimers: () => {
      for (const cb of [...timers.values()]) cb();
      timers.clear();
    },
    runFrames: () => {
      for (const cb of [...rafs.values()]) cb();
      rafs.clear();
    },
  };
}

test("a visible tab gets frames", () => {
  const f = fake();
  const clock = createFrameClock(f.env);
  let fired = 0;
  clock.schedule(() => fired++);
  expect(clock.pending()).toEqual([{ handle: 1, raf: true }]);
  f.runFrames();
  expect(fired).toBe(1);
  expect(clock.pending()).toEqual([]);
});

test("a tab that is ALREADY hidden gets a timer", () => {
  const f = fake();
  f.hide();
  const clock = createFrameClock(f.env);
  let fired = 0;
  clock.schedule(() => fired++);
  expect(clock.pending()).toEqual([{ handle: 1, raf: false }]);
  f.runTimers();
  expect(fired).toBe(1);
});

test("a frame in flight when the tab hides is rescued onto a timer", () => {
  const f = fake();
  const clock = createFrameClock(f.env);
  let fired = 0;
  clock.schedule(() => fired++);
  f.hide();
  clock.rescueHidden();
  expect(f.frames()).toBe(0); // the dead rAF was cancelled…
  expect(clock.pending()).toEqual([{ handle: 1, raf: false }]); // …and re-armed
  f.runTimers();
  expect(fired).toBe(1); // the frame the tab would have swallowed
});

test("a rescued frame keeps its handle, so cancel still reaches it", () => {
  const f = fake();
  const clock = createFrameClock(f.env);
  let fired = 0;
  const handle = clock.schedule(() => fired++);
  f.hide();
  clock.rescueHidden();
  clock.cancel(handle);
  expect(clock.pending()).toEqual([]);
  f.runTimers();
  expect(fired).toBe(0);
});

test("rescueHidden is idempotent and leaves timers alone", () => {
  const f = fake();
  f.hide();
  const clock = createFrameClock(f.env);
  clock.schedule(() => {});
  clock.rescueHidden();
  clock.rescueHidden();
  expect(clock.pending()).toEqual([{ handle: 1, raf: false }]);
});
