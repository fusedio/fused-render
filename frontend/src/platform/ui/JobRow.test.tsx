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
import { act, create } from "react-test-renderer";
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
  waiting_for: "",
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

/** All rendered text in a subtree, flattened — for assertions about what the
 *  row SAYS rather than which element says it. Added with D598, which moved
 *  the amount onto `.dl-status`: "no failure line" can no longer be checked by
 *  the element's absence, because a row with byte counts and no phase text
 *  legitimately draws one. */
function text(node: ReactTestRendererJSON | string | null): string {
  if (node === null) return "";
  if (typeof node === "string") return node;
  return (node.children ?? [])
    .map((c) => text(c as ReactTestRendererJSON | string))
    .join("");
}

function renderRow(job: Job, now?: number): ReactTestRendererJSON {
  const tree = create(<JobRow job={job} onChanged={() => {}} onPatch={() => {}} now={now} />).toJSON();
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
  // nothing. Shortened for display only: the full id stays reachable, since
  // two owners can ship the same name.
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

test("a done job draws a row with a working dismiss control (C1)", () => {
  // `JobRow` itself draws every state it is handed — a `done` job included.
  // What keeps a terminal job out of THIS file's own Jobs section is
  // `DownloadManagerView` only ever passing it `inFlightJobs`; `JobRow`
  // returning null for "done" left `RepoUpdatesDock.tsx`'s reuse of this same
  // component silently unable to draw the very rows Notifications exists to
  // hold (C1) — a done job filled the chip's circle and the panel's count
  // but drew no row and no ✕, permanently once D663 stopped sweeping it.
  const root = renderRow({ ...BASE, state: "done" });
  expect(findAll(root, "dl-row").length).toBeGreaterThan(0);
  expect(findAll(root, "dl-x")).toHaveLength(1);
});

test("an error job still draws — only a success clears itself", () => {
  const root = renderRow({ ...BASE, state: "error", message: "boom" });
  expect(findAll(root, "dl-row").length).toBeGreaterThan(0);
});

test("a cancelled job still draws — only a success clears itself", () => {
  const root = renderRow({ ...BASE, state: "cancelled" });
  expect(findAll(root, "dl-row").length).toBeGreaterThan(0);
});

// ---- a rejected Cancel/Dismiss must say so, not go quiet (D572) ----------------
// User: "the cancel button also doesn't seem to be doing anything?" — a click
// against a request that never lands (404/500/offline) used to hit an empty
// `catch` that discarded the failure outright: no toast, no console entry, no
// state change, no label move. This is the genuine coverage gap the bug report
// exposed — a green suite over an empty catch is exactly how a dead button
// ships. `cancelFn`/`dismissFn` are JobRow's own test seam (see its props'
// doc) rather than a `mock.module` on `@platform/lib/jobs`, which this
// file's sibling (DownloadManager.test.tsx) already documents as a
// contamination risk shared process-wide across `bun test`.
function pressButton(root: ReactTestRendererJSON, className: string): Promise<void> {
  const button = findAll(root, className)[0];
  const onClick = (button.props as { onClick: () => Promise<void> }).onClick;
  return act(async () => {
    await onClick();
  });
}

test("a rejected Cancel surfaces a failure message instead of going silent", async () => {
  const cancelFn = () => Promise.reject(new Error("network down"));
  const tree = create(
    // `detail: ""` so no PHASE text competes with the assertions below. The
    // line is still present before any click, because D598 moved the amount
    // (`jobAmount`) onto it and BASE carries one — so what this checks is that
    // it says nothing about a FAILURE yet, not that it is absent.
    <JobRow
      job={{ ...BASE, cancellable: true, state: "running", detail: "" }}
      onChanged={() => {}}
      onPatch={() => {}}
      cancelFn={cancelFn}
    />,
  );
  const before = tree.toJSON() as ReactTestRendererJSON;
  expect(text(before)).not.toContain("Could not cancel"); // nothing to say yet

  await pressButton(before, "dl-row-cancel");

  const after = tree.toJSON() as ReactTestRendererJSON;
  const status = findAll(after, "dl-status");
  expect(status).toHaveLength(1);
  expect(status[0].children).toEqual(["Could not cancel — check your connection and retry."]);
  // The row is untouched — no optimistic `cancel_requested` flip on a
  // request that never landed.
  expect(findAll(after, "dl-row-cancel")[0].children).toContain("Cancel");
});

test("a rejected Dismiss surfaces its own failure message and the row stays", async () => {
  const dismissFn = () => Promise.reject(new Error("network down"));
  // Stalled, not running: `canDismiss` requires `!running || job.stalled`,
  // and a stalled row is the one running-job case Dismiss (not Cancel) owns.
  const tree = create(
    <JobRow
      job={{ ...BASE, state: "running", stalled: true }}
      onChanged={() => {}}
      onPatch={() => {}}
      dismissFn={dismissFn}
    />,
  );
  const before = tree.toJSON() as ReactTestRendererJSON;

  await pressButton(before, "dl-x");

  const after = tree.toJSON() as ReactTestRendererJSON;
  // The row survives — `onPatch`'s filter never ran, so the parent's list is
  // unchanged, and JobRow itself still has a job to draw.
  expect(after).not.toBeNull();
  const status = findAll(after, "dl-status");
  expect(status).toHaveLength(1);
  expect(status[0].children).toEqual(["Could not dismiss — check your connection and retry."]);
});

test("a successful Cancel shows no failure line", async () => {
  const cancelFn = () => Promise.resolve({ ...BASE, cancel_requested: true });
  const tree = create(
    <JobRow
      job={{ ...BASE, cancellable: true, state: "running", detail: "" }}
      onChanged={() => {}}
      onPatch={() => {}}
      cancelFn={cancelFn}
    />,
  );
  const before = tree.toJSON() as ReactTestRendererJSON;

  await pressButton(before, "dl-row-cancel");

  // Asserted as the ABSENCE OF FAILURE TEXT, not as the absence of the line:
  // D598 moved the amount onto `.dl-status`, so a row with byte counts and no
  // phase text legitimately draws one. Checking for the element would make
  // this test fail for the right behaviour.
  const after = tree.toJSON() as ReactTestRendererJSON;
  expect(text(after)).not.toContain("Could not cancel");
});

test("a bare running row's fallback status measures against the caller's clock, not the browser's (C4)", () => {
  // No status text (no `detail`, no `message`) and no amount (`unit: ""`,
  // both `done`/`total` null): this is D665's fallback case, `jobDetail`,
  // which must read `now` off the `now` prop threaded from `useJobs`'s own
  // server-clock read, not off the browser's `Date.now()`.
  const now = 1_000_000;
  const root = renderRow(
    {
      ...BASE,
      detail: "",
      message: "",
      unit: "",
      done: null,
      total: null,
      started_at: now - 125, // 2m 5s ago, by the clock passed in
    },
    now,
  );
  expect(text(root)).toContain("started 2m ago");
});
