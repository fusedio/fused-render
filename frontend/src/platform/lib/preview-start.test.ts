import { expect, test } from "bun:test";

import { createPreviewStartQueue } from "./preview-start";

test("admits only the configured number of preview starts", () => {
  const queue = createPreviewStartQueue(2);
  const started: string[] = [];
  const releases: Array<() => void> = [];

  queue.request((release) => { started.push("a"); releases.push(release); });
  queue.request((release) => { started.push("b"); releases.push(release); });
  queue.request((release) => { started.push("c"); releases.push(release); });

  expect(started).toEqual(["a", "b"]);
  expect(queue.active()).toBe(2);
  expect(queue.pending()).toBe(1);

  releases[0]();
  expect(started).toEqual(["a", "b", "c"]);
  expect(queue.active()).toBe(2);
  expect(queue.pending()).toBe(0);
});

test("a priority preview is next without exceeding the limit", () => {
  const queue = createPreviewStartQueue(1);
  const started: string[] = [];
  let releaseFirst!: () => void;

  queue.request((release) => { started.push("first"); releaseFirst = release; });
  queue.request(() => started.push("background"));
  queue.request(() => started.push("hover"), true);

  releaseFirst();
  expect(started).toEqual(["first", "hover"]);
  expect(queue.pending()).toBe(1);
});

// The scroll case: cards a row or two ahead are queued before the one the
// reader is looking at, and the ranking is read when a slot frees — not when
// the request was made — so scrolling promotes the visible card without the
// component re-requesting (which would restart a running iframe).
test("a preview that becomes on-screen while queued is admitted first", () => {
  const queue = createPreviewStartQueue(1);
  const started: string[] = [];
  let releaseFirst!: () => void;
  let visible = false;

  queue.request((release) => { started.push("first"); releaseFirst = release; });
  queue.request(() => started.push("lookahead-a"));
  queue.request(() => started.push("scrolled-into-view"), () => visible);
  queue.request(() => started.push("lookahead-b"));

  visible = true;
  releaseFirst();
  expect(started).toEqual(["first", "scrolled-into-view"]);
  expect(queue.pending()).toBe(2);
});

test("a hover still outranks the previews that are merely on screen", () => {
  const queue = createPreviewStartQueue(1);
  const started: string[] = [];
  let releaseFirst!: () => void;

  queue.request((release) => { started.push("first"); releaseFirst = release; });
  queue.request(() => started.push("on-screen"), () => true);
  queue.request(() => started.push("hover"), true);

  releaseFirst();
  expect(started).toEqual(["first", "hover"]);
});

test("cancelling a queued or active preview frees its work", () => {
  const queue = createPreviewStartQueue(1);
  const started: string[] = [];
  const cancelFirst = queue.request(() => started.push("first"));
  const cancelSecond = queue.request(() => started.push("second"));

  cancelSecond();
  expect(queue.pending()).toBe(0);
  cancelFirst();
  expect(queue.active()).toBe(0);
  expect(started).toEqual(["first"]);
});
