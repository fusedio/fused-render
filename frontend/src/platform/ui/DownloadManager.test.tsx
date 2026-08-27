// The PARENT's behaviour, not JobRow's own (see JobRow.test.tsx for that half).
//
// PR #785 follow-up: JobRow alone returning null for a "done" job left every
// decision ABOVE it still counting the invisible row — the empty-card gate
// (`jobs.length === 0 && queued === 0`), the header's "N finished" tally, and
// `clearable` (which drew a Clear button for a row nobody could see). A
// successful job has to disappear from ALL of those, not just from its own
// row, or a lone success leaves an empty bordered box with a header and a
// Clear button over nothing — worse than the "done" row this was meant to
// remove.
//
// Rendered through `DownloadManagerView` — the pure, props-in half of this
// card, exported for exactly this file the way `JobRow` is exported for its
// own test — rather than the default-exported `DownloadManager`, which polls
// `/api/jobs` itself. Mounting THAT would mean mocking `@platform/lib/api`,
// which is shared by dozens of other test files; `mock.module` replaces it
// for the whole bun process, not just one file, and that contaminated an
// unrelated suite (`apps/ai_models/playground/client.test.ts`) the first
// time this file tried it. `DownloadManagerView` needs no such thing: no
// polling, no network, no `window`/`document`.
import { expect, test } from "bun:test";
import { act, create, type ReactTestRenderer, type ReactTestRendererJSON } from "react-test-renderer";

import { DownloadManagerView, type QueueSlot } from "@platform/ui/DownloadManager";
import type { Job } from "@platform/lib/jobs";

function findAll(node: ReactTestRendererJSON | null, className: string): ReactTestRendererJSON[] {
  if (node === null || typeof node === "string") return [];
  const hits: ReactTestRendererJSON[] = [];
  if (typeof node.props?.className === "string" && node.props.className.split(" ").includes(className)) {
    hits.push(node);
  }
  for (const child of node.children ?? []) {
    if (typeof child !== "string") hits.push(...findAll(child, className));
  }
  return hits;
}

function text(node: ReactTestRendererJSON | null): string {
  if (node === null) return "";
  if (typeof node === "string") return node;
  return (node.children ?? []).map((c) => text(c as ReactTestRendererJSON)).join("");
}

const BASE: Job = {
  id: "sys:ai-image:x",
  title: "a red fox",
  detail: "Saved x.png",
  model: "",
  kind: "task",
  state: "done",
  done: 28,
  total: 28,
  total_scope: "phase",
  unit: "",
  message: "",
  page: "",
  owner: "server",
  cancellable: true,
  cancel_requested: false,
  started_at: 0,
  updated_at: 0,
  finished_at: 0,
  stalled: false,
};

function renderCard(reported: Job[]): ReactTestRendererJSON | null {
  return create(
    <DownloadManagerView reported={reported} refresh={() => {}} patch={() => {}} />,
  ).toJSON() as ReactTestRendererJSON | null;
}

function fullQueue(queue: Partial<QueueSlot>): QueueSlot {
  return {
    waiting: 0,
    running: 0,
    rows: null,
    drawn: [],
    ...queue,
  };
}

function renderCardWithQueue(
  reported: Job[],
  queue: Partial<QueueSlot>,
): ReactTestRendererJSON | null {
  return create(
    <DownloadManagerView
      reported={reported}
      queue={fullQueue(queue)}
      refresh={() => {}}
      patch={() => {}}
    />,
  ).toJSON() as ReactTestRendererJSON | null;
}

// For tests that need to actually PRESS the toggle (collapsing is real
// component state, not a prop) rather than just inspect one static render.
function renderInstance(reported: Job[], queue?: Partial<QueueSlot>): ReactTestRenderer {
  return create(
    <DownloadManagerView
      reported={reported}
      queue={queue ? fullQueue(queue) : undefined}
      refresh={() => {}}
      patch={() => {}}
    />,
  );
}

test("a jobs list holding only a successful (done) job renders no card at all", () => {
  const tree = renderCard([{ ...BASE, id: "sys:ai-image:only-done" }]);
  expect(tree).toBeNull();
});

test("a done job beside a running one renders exactly one visible row and no Clear button", () => {
  const done = { ...BASE, id: "sys:ai-image:done" };
  const running: Job = {
    ...BASE,
    id: "sys:ai-image:running",
    state: "running",
    detail: "Denoising…",
    done: 2,
    total: 28,
  };
  const tree = renderCard([done, running]);
  expect(tree).not.toBeNull();
  expect(findAll(tree, "dl-row")).toHaveLength(1);
  expect(findAll(tree, "dl-clear")).toHaveLength(0);
});

test("an error job beside a done one still draws and is clearable — only the success vanishes", () => {
  const done = { ...BASE, id: "sys:ai-image:done" };
  const errored: Job = {
    ...BASE,
    id: "sys:ai-image:errored",
    state: "error",
    message: "boom",
  };
  const tree = renderCard([done, errored]);
  expect(tree).not.toBeNull();
  // Exactly the error row — the done one drew nothing.
  expect(findAll(tree, "dl-row")).toHaveLength(1);
  // A terminal row nobody can act on but Clear (it is not running, and it is
  // the only row the card is showing) — dismissible unlike the vanished one.
  expect(findAll(tree, "dl-clear")).toHaveLength(1);
});

test("a scheduled run's own terminal row still counts and draws — the exemption is for stand-ins, not for AI rows", () => {
  // A scheduled run's own outcome row (`sys:schedule:*`) deliberately does
  // NOT vanish on success like an ordinary AI row does — "a run appears,
  // works, and vanishes mid-sentence" is the bug that exists to prevent.
  // `isVanishedOnSuccess` must not blanket every "done" job or it would
  // re-break that.
  const scheduleDone: Job = { ...BASE, id: "sys:schedule:entry-1", title: "Nightly digest" };
  const tree = renderCard([scheduleDone]);
  expect(tree).not.toBeNull();
  expect(findAll(tree, "dl-row")).toHaveLength(1);
});

test("a stalled but still-running job alone offers no Clear button", () => {
  // D526: Clear used to count a stalled row as clearable — mirroring
  // clear_finished's own old bug — which meant pressing it could sweep the
  // RECORD of a job that was still genuinely running. The row itself is
  // still shown (dimmed via is-stalled); only the bulk button's count
  // changed.
  const stalled: Job = { ...BASE, id: "sys:ai-image:stalled", state: "running", stalled: true };
  const tree = renderCard([stalled]);
  expect(tree).not.toBeNull();
  expect(findAll(tree, "dl-row")).toHaveLength(1);
  expect(findAll(tree, "dl-clear")).toHaveLength(0);
});

test("a terminal row beside a stalled running one offers Clear, counting only the terminal one", () => {
  const stalled: Job = { ...BASE, id: "sys:ai-image:stalled", state: "running", stalled: true };
  const done = { ...BASE, id: "sys:ai-image:errored", state: "error" as const, message: "boom" };
  const tree = renderCard([stalled, done]);
  expect(tree).not.toBeNull();
  expect(findAll(tree, "dl-row")).toHaveLength(2);
  expect(findAll(tree, "dl-clear")).toHaveLength(1);
});

// -------------------------------------------------- the collapse toggle (D548)
//
// Reversed by user call (2026-08-27): "there should be nothing called a
// 'non foldable card' — everything is foldable, even for the job cards."
// D526/D527's whole premise — SOME rows (the queue's, a live schedule
// stand-in) were exempt from the fold, so the toggle sometimes had nothing
// to hide and was drawn as an inert `<span>` — is gone. The toggle is ALWAYS
// a real `<button>`, and collapsing ALWAYS hides every row. Reachability
// while collapsed is the header's job now: `queue.cancelAll` drops its
// threshold to one row (queue-dock-lib.test.ts owns that rule), and the
// header keeps naming what is hidden (`jobsSummary`) and keeps the overall
// progress bar.

function clickToggle(renderer: ReactTestRenderer) {
  const before = renderer.toJSON() as ReactTestRendererJSON;
  const toggle = findAll(before, "dl-toggle")[0];
  act(() => {
    (toggle.props as { onClick: () => void }).onClick();
  });
}

test("the toggle is a real button even with only queue rows to fold", () => {
  const tree = renderCardWithQueue([], { waiting: 1, running: 0 });
  expect(tree).not.toBeNull();
  const toggles = findAll(tree, "dl-toggle");
  expect(toggles).toHaveLength(1);
  expect(toggles[0].type).toBe("button");
});

test("the toggle is a real button even with only a lone scheduled stand-in row", () => {
  const liveSchedule: Job = {
    ...BASE,
    id: "sys:schedule:entry-1",
    state: "running",
    stalled: false,
  };
  const tree = renderCard([liveSchedule]);
  expect(tree).not.toBeNull();
  const toggles = findAll(tree, "dl-toggle");
  expect(toggles).toHaveLength(1);
  expect(toggles[0].type).toBe("button");
});

test("collapsing hides every row — queue rows and job rows alike, no exemption", () => {
  const running: Job = { ...BASE, id: "sys:ai-image:running", state: "running", stalled: false };
  const renderer = renderInstance([running], {
    waiting: 0,
    running: 0,
    rows: <div className="q-row">a queued message</div>,
  });

  const before = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(before, "dl-row")).toHaveLength(1);
  expect(findAll(before, "q-row")).toHaveLength(1);

  clickToggle(renderer);

  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "dl-row")).toHaveLength(0);
  expect(findAll(after, "q-row")).toHaveLength(0);
  expect(findAll(after, "dl-rows")).toHaveLength(0); // no empty box left behind
});

test("the header still names the hidden work once collapsed", () => {
  const running: Job = { ...BASE, id: "sys:ai-image:running", state: "running", stalled: false };
  const renderer = renderInstance([running]);
  const before = text(findAll(renderer.toJSON() as ReactTestRendererJSON, "dl-summary")[0]);
  clickToggle(renderer);
  const after = text(findAll(renderer.toJSON() as ReactTestRendererJSON, "dl-summary")[0]);
  expect(before).toBe(after);
  expect(after.length).toBeGreaterThan(0);
});

test("the overall progress bar survives the collapse while something is running", () => {
  const running: Job = {
    ...BASE,
    id: "sys:ai-image:running",
    state: "running",
    done: 5,
    total: 10,
    stalled: false,
  };
  const renderer = renderInstance([running]);
  clickToggle(renderer);
  const after = renderer.toJSON() as ReactTestRendererJSON;
  // Direct child of .dl-host, per notifications.css's own ".dl-host > .dl-bar"
  // selector — distinguishes the header's collapsed-state bar from any bar a
  // (now-hidden) row would have drawn.
  expect((after.children ?? []).some((c) => {
    const child = c as ReactTestRendererJSON;
    return typeof child !== "string" && child.props?.className?.split(" ").includes("dl-bar");
  })).toBe(true);
});

test("Cancel all is offered for a single queued row once the card is collapsed (D548)", () => {
  // queue-dock-lib.ts's `showCancelAll` owns the actual threshold (its own
  // test pins 2+ expanded, 1+ collapsed); this just confirms the CARD calls
  // `cancelAll` as a function of `collapsed` rather than rendering a
  // pre-decided node, which is the only way that threshold can reach here.
  const cancelAll = (collapsed: boolean) =>
    collapsed ? <button className="q-all">Cancel all</button> : null;
  const renderer = renderInstance([], { waiting: 1, running: 0, cancelAll });

  const before = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(before, "q-all")).toHaveLength(0);

  clickToggle(renderer);

  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "q-all")).toHaveLength(1);
});
