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
import { create, type ReactTestRendererJSON } from "react-test-renderer";

import { DownloadManagerView } from "@platform/ui/DownloadManager";
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
  // jobs.ts `foldedJobRows` deliberately keeps a scheduled run's outcome row
  // (`sys:schedule:*`) through the fold — "a run appears, works, and vanishes
  // mid-sentence" is the bug that exists to prevent. `isVanishedOnSuccess`
  // must not blanket every "done" job or it would re-break that.
  const scheduleDone: Job = { ...BASE, id: "sys:schedule:entry-1", title: "Nightly digest" };
  const tree = renderCard([scheduleDone]);
  expect(tree).not.toBeNull();
  expect(findAll(tree, "dl-row")).toHaveLength(1);
});
