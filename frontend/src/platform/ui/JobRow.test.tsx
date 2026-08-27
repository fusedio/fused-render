// JobRow's model suffix (this follow-up): the model reaching the UI as its
// own field (jobs.py `Job.model`) is only half the fix — it also has to
// render as a SEPARATE, dimmed element on the title row (`.dl-model`), never
// folded into `.dl-title` itself, or the truncation guarantee the whole
// feature exists for (a long prompt must never squeeze the model out of
// view) would have nothing to hang off of.
//
// react-test-renderer, the same tool `hook-harness.ts` uses: this suite has
// no DOM, and it is the only thing here that can render a real component
// (rather than call a pure function) with no document at all.
import { expect, test } from "bun:test";
import { create } from "react-test-renderer";
import type { ReactTestRendererJSON } from "react-test-renderer";

import { JobRow } from "@platform/ui/DownloadManager";
import type { Job } from "@platform/lib/jobs";

const BASE: Job = {
  id: "a",
  title: "a red fox in snow",
  detail: "Denoising — step 2/4 · ~2s left",
  model: "",
  kind: "task",
  state: "running",
  done: 45,
  total: 100,
  total_scope: "phase",
  unit: "",
  message: "",
  page: "",
  owner: "page",
  cancellable: true,
  cancel_requested: false,
  started_at: 0,
  updated_at: 0,
  finished_at: null,
  stalled: false,
};

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

function renderRow(job: Job): ReactTestRendererJSON {
  const tree = create(<JobRow job={job} onChanged={() => {}} onPatch={() => {}} />).toJSON();
  if (tree === null || Array.isArray(tree)) throw new Error("JobRow did not render a single root node");
  return tree;
}

test("a job with a model draws a dimmed .dl-model suffix after the title", () => {
  const root = renderRow({ ...BASE, model: "FLUX.1-schnell" });
  const model = findAll(root, "dl-model");
  expect(model).toHaveLength(1);
  expect(model[0].children).toEqual(["FLUX.1-schnell"]);
});

test("an owner/model repo id draws only the model half, with the full id on hover", () => {
  // The owner prefix is identical for every row a given model ever draws, so it
  // consumed the head's scarcest space on the one part that distinguishes
  // nothing — and `.dl-model` is `flex: 0 0 auto`, so what it took came out of
  // `.dl-title`, ellipsizing the user's prompt away. Shortened for display
  // only: the full id stays reachable, since two owners can ship the same name.
  const root = renderRow({ ...BASE, model: "black-forest-labs/FLUX.2-klein-4B" });
  const model = findAll(root, "dl-model");
  expect(model).toHaveLength(1);
  expect(model[0].children).toEqual(["FLUX.2-klein-4B"]);
  expect(model[0].props.title).toBe("black-forest-labs/FLUX.2-klein-4B");
});

test("a job with no model renders no .dl-model element at all — no empty span, no stray gap", () => {
  const root = renderRow({ ...BASE, model: "" });
  expect(findAll(root, "dl-model")).toHaveLength(0);
});

test("the title keeps its own text regardless of the model — model is a sibling, not a concatenation", () => {
  const root = renderRow({ ...BASE, model: "FLUX.1-schnell" });
  const title = findAll(root, "dl-title");
  expect(title).toHaveLength(1);
  expect(title[0].children).toEqual(["a red fox in snow"]);
});

test("a model equal to the title draws no .dl-model suffix — a load row must not repeat the model twice", () => {
  // `_start_resident`/`load` (fused_render/ai/supervisor.py) set both `title`
  // and `model` to the model id, so a load row's title IS the model — the
  // suffix would otherwise repeat it verbatim right next to itself.
  const root = renderRow({ ...BASE, title: "org/model", model: "org/model" });
  expect(findAll(root, "dl-model")).toHaveLength(0);
});

test("a done job draws nothing at all — success clears itself from the corner", () => {
  // PR #785: success used to be cleared server-side (supervisor._finish
  // dismissed the row instantly), which raced every poller including
  // fused_ai.py's own job watcher. The clearing now happens here instead —
  // the server reports a real, observable "done" state and the frontend
  // simply does not draw it. `error`/`cancelled` rows are NOT this: they
  // must stay visible until the user dismisses them.
  const tree = create(<JobRow job={{ ...BASE, state: "done" }} onChanged={() => {}} onPatch={() => {}} />).toJSON();
  expect(tree).toBeNull();
});

test("an error job still draws — only a success clears itself", () => {
  const root = renderRow({ ...BASE, state: "error", message: "boom" });
  expect(findAll(root, "dl-row").length).toBeGreaterThan(0);
});

test("a cancelled job still draws — only a success clears itself", () => {
  const root = renderRow({ ...BASE, state: "cancelled" });
  expect(findAll(root, "dl-row").length).toBeGreaterThan(0);
});
