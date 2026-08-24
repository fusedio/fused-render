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
