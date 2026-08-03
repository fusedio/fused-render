// The deferred-close primitive every overlay's exit animation is built on.
// Dialogs and the slide-over are unmounted by their CALLER (`{open && <Modal/>}`),
// so an overlay cannot delay its own removal — it can only delay the onClose
// that triggers it. createCloseDeferrer owns that delay.
import { expect, test } from "bun:test";

import { createCloseDeferrer } from "./exit-animation";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const DUR = 40;

test("holds the close for the exit duration, announcing the closing phase first", async () => {
  const phases: boolean[] = [];
  let closed = 0;
  const d = createCloseDeferrer(DUR, () => closed++, (c) => phases.push(c));

  d.request();
  // The caller must not unmount yet — this is the frame the exit runs in.
  expect(closed).toBe(0);
  expect(phases).toEqual([true]);
  expect(d.closing).toBe(true);

  await sleep(DUR + 40);
  expect(closed).toBe(1);
  expect(d.closing).toBe(false);
});

test("repeated requests collapse into one close", async () => {
  let closed = 0;
  const d = createCloseDeferrer(DUR, () => closed++, () => {});
  d.request(); // ✕
  d.request(); // Esc landing during the animation
  d.request(); // backdrop click too
  await sleep(DUR + 40);
  expect(closed).toBe(1);
});

test("a request after the close completed starts a fresh exit", async () => {
  let closed = 0;
  const d = createCloseDeferrer(DUR, () => closed++, () => {});
  d.request();
  await sleep(DUR + 40);
  d.request();
  expect(d.closing).toBe(true);
  await sleep(DUR + 40);
  expect(closed).toBe(2);
});

test("cancel drops a pending close and leaves the phase clean", async () => {
  const phases: boolean[] = [];
  let closed = 0;
  const d = createCloseDeferrer(DUR, () => closed++, (c) => phases.push(c));
  d.request();
  d.cancel(); // the overlay unmounted for another reason (a navigation)
  expect(d.closing).toBe(false);
  expect(phases).toEqual([true, false]);
  await sleep(DUR + 40);
  expect(closed).toBe(0);
});

test("cancel with nothing pending is a no-op", () => {
  const phases: boolean[] = [];
  const d = createCloseDeferrer(DUR, () => {}, (c) => phases.push(c));
  d.cancel();
  expect(phases).toEqual([]);
  expect(d.closing).toBe(false);
});

test("a zero duration still defers past the current frame, never inline", async () => {
  // Reduced motion / a zero-duration caller must not turn request() into a
  // synchronous onClose: callers unmount in that callback, and unmounting from
  // inside an event handler that is still reading the node is the bug this
  // whole indirection avoids.
  let closed = 0;
  const d = createCloseDeferrer(0, () => closed++, () => {});
  d.request();
  expect(closed).toBe(0);
  await sleep(20);
  expect(closed).toBe(1);
});
