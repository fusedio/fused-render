// Bulk archive on the List's grouped views: the heading checkbox, the bar, the
// confirm, the Undo. The rows are stand-ins drawn through `groupFrames`'s own
// `rowAt`, so the heading and its cap are the real ones.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, mock, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { Task } from "@platform/lib/api";

const { groupFrames, PROJECT_GROUP_ROW_CAP } = await import("./ScheduleTaskViews");
const { BulkBar, RowCheck, useBulkArchive } = await import("./TaskBulk");
const { selectableTask } = await import("./task-selection");

const realFetch = globalThis.fetch;
const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  globalThis.fetch = realFetch;
});

const mk = (n: number, over: Partial<Task> = {}): Task[] =>
  Array.from({ length: n }, (_, i) => ({ key: `t${i}`, title: `T ${i}`, status: "done", ...over }) as Task);

function serve() {
  const posts: { url: string; key: string }[] = [];
  globalThis.fetch = mock(async (url: string, init?: RequestInit) => {
    posts.push({ url, key: JSON.parse(String(init?.body)).key });
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  }) as unknown as typeof fetch;
  return posts;
}

function Harness({ tasks, by = "project", resetKey = "" }: { tasks: Task[]; by?: string; resetKey?: string }) {
  const h = useBulkArchive({ tasks, selectable: selectableTask, resetKey });
  const groups = [{ key: "/p", label: "p", rows: h.tasks, recentAt: 0 }] as never;
  return createElement(
    "div",
    null,
    createElement(BulkBar, { count: h.selection.size, onArchive: h.archiveSelected, onClear: h.selection.clear }),
    groupFrames(
      groups,
      (t) => createElement("div", { key: t.key, className: "row" }, createElement(RowCheck, { select: h.bulk, task: t })),
      by as never, new Set(), undefined, () => {}, new Set(), () => {}, h.bulk,
    ),
    h.node,
    createElement("i", { id: "n" }, String(h.selection.size)),
  );
}

async function render(tasks: Task[], resetKey = "") {
  let r!: ReactTestRenderer;
  await act(async () => { r = create(createElement(Harness, { tasks, resetKey })); });
  mounted.push(r);
  return r;
}
const by = (r: ReactTestRenderer, c: string) =>
  r.root.findAll((n) => typeof n.type === "string" && typeof n.props.className === "string" && n.props.className.split(" ").includes(c));
const txt = (n: { children: unknown[] }): string =>
  n.children.map((c) => (typeof c === "string" ? c : txt(c as { children: unknown[] }))).join("");
const call = async (el: { props: Record<string, (...a: unknown[]) => void> }, fn = "onClick") =>
  act(async () => { el.props[fn]({ stopPropagation() {} }); });
const bar = (r: ReactTestRenderer, label: string) => by(r, "tasks-bulk-btn").find((b) => txt(b) === label)!;
const keys = (posts: { url: string; key: string }[], url: string) => posts.filter((p) => p.url === url).map((p) => p.key).sort();

test("the heading checkbox selects the whole group, rows past the 10-row cap included", async () => {
  const r = await render(mk(15));
  expect(by(r, "row")).toHaveLength(PROJECT_GROUP_ROW_CAP);
  await call(by(r, "tasks-group-check")[0] as never, "onChange");
  expect(txt(by(r, "tasks-bulk-count")[0])).toBe("15 selected");
});

test("the heading checkbox is indeterminate with some, and clears the group when all are in", async () => {
  const r = await render(mk(3));
  await call(by(r, "tasks-row-check")[0] as never, "onChange");
  expect(txt(by(r, "tasks-bulk-count")[0])).toBe("1 selected");
  expect(by(r, "tasks-group-check")[0].props.checked).toBe(false);
  await call(by(r, "tasks-group-check")[0] as never, "onChange");
  expect(by(r, "tasks-group-check")[0].props.checked).toBe(true);
  await call(by(r, "tasks-group-check")[0] as never, "onChange");
  expect(by(r, "tasks-bulk-bar")).toHaveLength(0);
});

test("a row checkbox does not reach the row's own press", async () => {
  const r = await render(mk(2));
  let stopped = false;
  await act(async () => by(r, "tasks-row-check")[0].props.onClick({ stopPropagation: () => (stopped = true) }));
  expect(stopped).toBe(true);
});

test("Archive calls archive once per key, then one Undo calls unarchive per key", async () => {
  const posts = serve();
  const r = await render(mk(4));
  await call(by(r, "tasks-group-check")[0] as never, "onChange");
  await call(bar(r, "Archive"));
  expect(keys(posts, "/api/tasks/archive")).toEqual(["t0", "t1", "t2", "t3"]);
  expect(by(r, "row")).toHaveLength(0);
  expect(by(r, "ws-undo")).toHaveLength(1);
  expect(txt(by(r, "ws-undo-text")[0])).toBe("Archived 4 tasks");
  await call(by(r, "ws-undo-btn")[0] as never);
  expect(keys(posts, "/api/tasks/unarchive")).toEqual(["t0", "t1", "t2", "t3"]);
  expect(by(r, "row")).toHaveLength(4);
});

test("a running task asks first, naming how many are running", async () => {
  const posts = serve();
  const r = await render([...mk(2), ...mk(1, { key: "run", status: "in_progress" })]);
  await call(by(r, "tasks-group-check")[0] as never, "onChange");
  await call(bar(r, "Archive"));
  expect(keys(posts, "/api/tasks/archive")).toEqual([]);
  const dlg = r.root.findAllByProps({ title: "Archive tasks" })[0];
  expect(dlg.props.message).toBe("Archive 3 tasks? 1 is still running and will be stopped.");
  await act(async () => dlg.props.onConfirm());
  expect(keys(posts, "/api/tasks/archive")).toEqual(["run", "t0", "t1"]);
});

test("the selection drops tasks that leave the list and empties when the reset key moves", async () => {
  const r = await render(mk(3));
  await call(by(r, "tasks-group-check")[0] as never, "onChange");
  await act(async () => r.update(createElement(Harness, { tasks: mk(2) })));
  expect(txt(by(r, "tasks-bulk-count")[0])).toBe("2 selected");
  await act(async () => r.update(createElement(Harness, { tasks: mk(2), resetKey: "search=x" })));
  expect(by(r, "tasks-bulk-bar")).toHaveLength(0);
});
