// The toast queue's exit path. A dismissed toast is not removed from the queue
// straight away: it is flagged `leaving` for the length of the exit animation
// so the card can fade + collapse and the toasts below it glide up instead of
// snapping. Both dismiss routes — the TTL timer and a manual ✕ — go through it.
import { afterEach, beforeEach, expect, test } from "bun:test";

// toast.ts schedules through `window` (browser code); bun's test runtime has no
// DOM, so point window at the global timers.
(globalThis as { window?: unknown }).window ??= globalThis;

import { TOAST_EXIT_MS, dismissToast, getToasts, pushToast } from "@platform/lib/toast";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
// One exit window plus slack for timer jitter.
const afterExit = () => sleep(TOAST_EXIT_MS + 60);

// Dismissal is deliberately not instant, so draining the queue between tests
// has to wait out the exit window too.
async function clearAll(): Promise<void> {
  if (getToasts().length === 0) return;
  for (const t of getToasts()) dismissToast(t.id);
  await afterExit();
}

beforeEach(clearAll);
afterEach(clearAll);

test("a manual dismiss flags the toast leaving, then removes it", async () => {
  const id = pushToast({ msg: "Path copied", tone: "info", ttlMs: 0 });
  expect(getToasts().map((t) => [t.id, t.leaving])).toEqual([[id, false]]);

  dismissToast(id);
  // Still rendered — this is the frame the exit animation runs in.
  expect(getToasts().map((t) => [t.id, t.leaving])).toEqual([[id, true]]);

  await afterExit();
  expect(getToasts()).toEqual([]);
});

test("the TTL dismiss takes the same leaving path", async () => {
  const id = pushToast({ msg: "expires", tone: "info", ttlMs: 10 });
  await sleep(40);
  const leaving = getToasts();
  expect(leaving.map((t) => [t.id, t.leaving])).toEqual([[id, true]]);
  await afterExit();
  expect(getToasts()).toEqual([]);
});

test("dismissing twice does not shorten or restart the exit", async () => {
  const id = pushToast({ msg: "x", tone: "error", ttlMs: 0 });
  dismissToast(id);
  await sleep(TOAST_EXIT_MS / 2);
  dismissToast(id); // a second ✕ click / a TTL landing mid-exit
  expect(getToasts().map((t) => t.id)).toEqual([id]);
  await afterExit();
  expect(getToasts()).toEqual([]);
});

test("a leaving toast does not block later ones from arriving or expiring", async () => {
  const first = pushToast({ msg: "first", tone: "info", ttlMs: 0 });
  dismissToast(first);
  const second = pushToast({ msg: "second", tone: "info", ttlMs: 0 });
  // Order is preserved: the leaving card keeps its slot while it collapses.
  expect(getToasts().map((t) => t.id)).toEqual([first, second]);
  await afterExit();
  expect(getToasts().map((t) => t.id)).toEqual([second]);
});

test("dismissing an unknown id is a no-op", () => {
  const id = pushToast({ msg: "x", tone: "info", ttlMs: 0 });
  dismissToast(id + 999);
  expect(getToasts().map((t) => [t.id, t.leaving])).toEqual([[id, false]]);
});
