// The toast queue's exit path and stack cap. A dismissed toast is not removed
// from the queue straight away: it is flagged `leaving` for the length of the
// exit animation so the card can fade + collapse and the toasts below it
// glide up instead of snapping. There is no TTL any more (Task 5) — a toast
// stays until the ✕ or the code that raised it dismisses it, so the only
// route into `leaving` is dismissToast itself.
import { afterEach, beforeEach, expect, test } from "bun:test";

import { installDomShim } from "@platform/lib/testDomShim";

// toast.ts schedules through `window` (browser code); bun's test runtime has
// no DOM. See testDomShim.ts for why this is the one shared stub every suite
// in the run installs, rather than a stub hand-rolled per file.
installDomShim();

import { MAX_TOASTS, TOAST_EXIT_MS, dismissToast, getToasts, pushToast } from "@platform/lib/toast";

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
  const id = pushToast({ msg: "Path copied", tone: "info" });
  expect(getToasts().map((t) => [t.id, t.leaving])).toEqual([[id, false]]);

  dismissToast(id);
  // Still rendered — this is the frame the exit animation runs in.
  expect(getToasts().map((t) => [t.id, t.leaving])).toEqual([[id, true]]);

  await afterExit();
  expect(getToasts()).toEqual([]);
});

test("a toast never dismisses itself — it is still there well past the old TTL window", async () => {
  const id = pushToast({ msg: "stays until cleared", tone: "info" });
  await sleep(50);
  expect(getToasts().map((t) => [t.id, t.leaving])).toEqual([[id, false]]);
});

test("dismissing twice does not shorten or restart the exit", async () => {
  const id = pushToast({ msg: "x", tone: "error" });
  dismissToast(id);
  await sleep(TOAST_EXIT_MS / 2);
  dismissToast(id); // a second ✕ click landing mid-exit
  expect(getToasts().map((t) => t.id)).toEqual([id]);
  await afterExit();
  expect(getToasts()).toEqual([]);
});

test("a leaving toast does not block later ones from arriving", async () => {
  const first = pushToast({ msg: "first", tone: "info" });
  dismissToast(first);
  const second = pushToast({ msg: "second", tone: "info" });
  // Order is preserved: the leaving card keeps its slot while it collapses.
  expect(getToasts().map((t) => t.id)).toEqual([first, second]);
  await afterExit();
  expect(getToasts().map((t) => t.id)).toEqual([second]);
});

test("dismissing an unknown id is a no-op", () => {
  const id = pushToast({ msg: "x", tone: "info" });
  dismissToast(id + 999);
  expect(getToasts().map((t) => [t.id, t.leaving])).toEqual([[id, false]]);
});

// ---- the stack caps at MAX_TOASTS (Task 5) --------------------------------

test("a 6th toast drops the oldest live one, keeping the stack at MAX_TOASTS", () => {
  const ids = Array.from({ length: MAX_TOASTS }, (_, i) => pushToast({ msg: `t${i}`, tone: "info" }));
  expect(getToasts().map((t) => t.id)).toEqual(ids);

  const sixth = pushToast({ msg: "t5", tone: "info" });
  const live = getToasts().map((t) => t.id);
  expect(live).toHaveLength(MAX_TOASTS);
  expect(live).not.toContain(ids[0]); // the oldest was dropped
  expect(live).toEqual([...ids.slice(1), sixth]);
});

test("a toast already animating out does not count toward the cap, and is not double-dropped", async () => {
  const ids = Array.from({ length: MAX_TOASTS }, (_, i) => pushToast({ msg: `t${i}`, tone: "info" }));
  dismissToast(ids[0]); // leaving, but still in the array
  const sixth = pushToast({ msg: "t5", tone: "info" });
  // 5 live + 1 leaving fits: only a LIVE overflow past MAX_TOASTS is dropped.
  const all = getToasts().map((t) => t.id);
  expect(all).toEqual([...ids, sixth]);
  await afterExit();
  expect(getToasts().map((t) => t.id)).toEqual([...ids.slice(1), sixth]);
});
