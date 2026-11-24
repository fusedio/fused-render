// THE RING THAT FILES THE TASK (TaskFileSeat) under a head that walks tasks:
// a confirm is about one task, an answer that lands after a walk does not move
// the head, and a cancelled confirm spent no press.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { Task } from "@platform/lib/api";

const { TaskFileSeat } = await import("./TaskFileSeat");

const realFetch = globalThis.fetch;
let posted: string[] = [];
let release: () => void = () => {};
let hold = false;
beforeEach(() => {
  posted = [];
  hold = false;
  (globalThis as { fetch: unknown }).fetch = (input: unknown) => {
    posted.push(String(input));
    const res = { ok: true, status: 200, json: async () => ({}) } as unknown as Response;
    if (!hold) return Promise.resolve(res);
    return new Promise<Response>((done) => {
      release = () => done(res);
    });
  };
});
const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  globalThis.fetch = realFetch;
});

const job = { count: 1, run_id: "r", jobs: [{ id: "j", title: "" }] };
const task = (key: string, over: Partial<Task> = {}): Task =>
  ({ key, task_id: key, title: key, project: "/p", target: "/p", session_id: key, status: "done", ...over }) as Task;
/** A Done task with a live background job: its Archive asks first. */
const asking = (key: string) => task(key, { background_jobs: job } as Partial<Task>);

function render(props: Parameters<typeof TaskFileSeat>[0]) {
  let r!: ReactTestRenderer;
  act(() => {
    r = create(createElement(TaskFileSeat, props));
  });
  mounted.push(r);
  return r;
}
const button = (r: ReactTestRenderer) => r.root.findByProps({ className: "task-head-file tasks-act--archive" });
const confirm = (r: ReactTestRenderer) => r.root.findAllByProps({ title: "Archive task" })[0];
const settle = () =>
  act(async () => {
    await new Promise((done) => setTimeout(done, 0));
  });

test("a confirm open for A is dropped when the head walks to B — confirming can never archive A", () => {
  const r = render({ task: asking("A") });
  act(() => button(r).props.onClick({ stopPropagation() {} }));
  expect(confirm(r)).toBeDefined();
  act(() => r.update(createElement(TaskFileSeat, { task: asking("B") })));
  expect(confirm(r)).toBeUndefined();
  expect(posted).toEqual([]);
});

test("a cancelled confirm spends no press; a confirmed one does, as the filing starts", async () => {
  let pressed = 0;
  const r = render({ task: asking("A"), onPress: () => (pressed += 1) });
  act(() => button(r).props.onClick({ stopPropagation() {} }));
  expect(pressed).toBe(0);
  act(() => confirm(r)!.props.onCancel());
  expect(pressed).toBe(0);
  act(() => button(r).props.onClick({ stopPropagation() {} }));
  await act(async () => {
    confirm(r)!.props.onConfirm();
    await new Promise((done) => setTimeout(done, 0));
  });
  expect(pressed).toBe(1);
  expect(posted).toContain("/api/tasks/archive");
});

test("an answer that lands after the head walked on re-reads but does not move the head", async () => {
  hold = true;
  const filed: string[] = [];
  const r = render({ task: task("A"), onFiled: (k) => filed.push(k) });
  act(() => button(r).props.onClick({ stopPropagation() {} }));
  expect(posted).toContain("/api/tasks/archive");
  act(() => r.update(createElement(TaskFileSeat, { task: task("B"), onFiled: (k) => filed.push(k) })));
  release();
  await settle();
  expect(filed).toEqual([]);
  // …and on the task it was made for, it does.
  hold = false;
  act(() => button(r).props.onClick({ stopPropagation() {} }));
  await settle();
  expect(filed).toEqual(["archive"]);
});

test("an answer that lands after the seat went away moves nothing", async () => {
  hold = true;
  const filed: string[] = [];
  const r = render({ task: task("A"), onFiled: (k) => filed.push(k) });
  act(() => button(r).props.onClick({ stopPropagation() {} }));
  act(() => r.unmount());
  mounted.splice(mounted.indexOf(r), 1);
  release();
  await settle();
  expect(filed).toEqual([]);
});
