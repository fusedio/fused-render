// THE TASKS PAGE'S PANEL HEAD, behind the shared-head flag (task-head-flag;
// design.md "One task head, everywhere", PR 1). Mounted, not grepped: with the
// flag ON the panel's head is `TaskHead` — × with its Esc hint, the chevrons,
// the identity, ↗ Open, and the chat's own ⋮ (`Kebab`, "More options"); with it
// OFF it is the head this panel has always drawn, its own ⋮ ("More actions").
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";
import type { Task } from "@platform/lib/api";

const { TaskPeek } = await import("./TaskPeek");
const store = await import("./task-peek-store");
const { publishTaskHeadUnified, resetTaskHeadUnifiedFlagForTests } = await import("./task-head-flag");

const MISSING = Symbol("missing");
const g = globalThis as Record<string, unknown>;
const saved = new Map<string, unknown>();
const inert = class {
  observe() {}
  unobserve() {}
  disconnect() {}
};
const realFetch = globalThis.fetch;
beforeEach(() => {
  const stubs: Record<string, unknown> = {
    ResizeObserver: inert,
    MutationObserver: inert,
    getComputedStyle: () => ({
      display: "block",
      position: "static",
      columnGap: "0",
      paddingLeft: "0",
      paddingRight: "0",
      getPropertyValue: () => "",
    }),
  };
  for (const [name, stub] of Object.entries(stubs)) {
    saved.set(name, name in g ? g[name] : MISSING);
    g[name] = stub;
  }
  // Every read the panel and the ⋮ make (the chat's template, the listing)
  // answers empty: this suite is about the head, not what is behind it.
  (globalThis as { fetch: unknown }).fetch = async () =>
    ({ ok: true, status: 200, json: async () => ({ tasks: [] }) }) as unknown as Response;
  resetTaskHeadUnifiedFlagForTests();
  store.resetPeekStoreForTests();
});
const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  for (const [name, prior] of saved) {
    if (prior === MISSING) delete g[name];
    else g[name] = prior;
  }
  saved.clear();
  globalThis.fetch = realFetch;
  resetTaskHeadUnifiedFlagForTests();
  store.resetPeekStoreForTests();
});

const task = (over: Partial<Task> = {}): Task =>
  ({
    key: "sess-1",
    task_id: "TASK-042",
    title: "Rename the pane noun",
    project: "/repo/app",
    target: "/repo/app",
    session_id: "sess-1",
    status: "done",
    ...over,
  }) as Task;

/** As much of an element as the panel touches on mount: a box, no children,
 *  listeners that hear nothing. */
function fakeEl() {
  return {
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 600, height: 800, right: 600, bottom: 800, x: 0, y: 0 }),
    clientWidth: 600,
    clientHeight: 800,
    children: [],
    parentElement: null,
    style: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    addEventListener() {},
    removeEventListener() {},
    querySelector: () => null,
    querySelectorAll: () => [],
    toggleAttribute() {},
    setAttribute() {},
    removeAttribute() {},
    focus() {},
    contains: () => false,
    contentDocument: null,
  };
}

async function open(tasks: Task[]) {
  let r!: ReactTestRenderer;
  act(() => {
    store.setPeekHost(true);
    store.openPeek(tasks[0]!.key);
  });
  act(() => {
    r = create(createElement(TaskPeek, { tasks, loaded: true }), { createNodeMock: () => fakeEl() });
  });
  mounted.push(r);
  // The reads the mount started (the chat's template, the ⋮'s listing) land
  // inside `act`, so their state updates are flushed rather than warned about.
  for (let i = 0; i < 3; i += 1) {
    await act(async () => {
      await new Promise((done) => setTimeout(done, 0));
    });
  }
  return r;
}
const hostAll = (r: ReactTestRenderer, pick: (n: ReactTestInstance) => boolean) =>
  r.root.findAll((n) => typeof n.type === "string" && pick(n));
const byLabel = (r: ReactTestRenderer, label: string) => hostAll(r, (n) => n.props["aria-label"] === label)[0];
const hasClass = (n: ReactTestInstance, c: string) => String(n.props.className ?? "").split(" ").includes(c);

test("flag ON: the shared head — ×, chevrons, identity, ↗ Open and the chat's own ⋮", async () => {
  publishTaskHeadUnified({ enabled: true });
  const r = await open([task()]);
  expect(hostAll(r, (n) => hasClass(n, "task-head"))).toHaveLength(1);
  expect(hostAll(r, (n) => hasClass(n, "task-side-peek-head"))).toHaveLength(0);
  const close = byLabel(r, "Close the task panel")!;
  expect(close.props["data-hint"]).toBe("Close · Esc");
  expect(byLabel(r, "Previous task")).toBeDefined();
  expect(byLabel(r, "Next task")).toBeDefined();
  const door = hostAll(r, (n) => hasClass(n, "task-head-open"))[0]!;
  expect(door.props["aria-label"]).toBe("Open in Explorer");
  // The ⋮ is the chat's `Kebab` — the same menu every other task view opens —
  // and the panel's own "More actions" button is gone.
  expect(byLabel(r, "More options")).toBeDefined();
  expect(byLabel(r, "More actions")).toBeUndefined();
  // The worktree seat is there for the chat to portal into.
  expect(hostAll(r, (n) => hasClass(n, "task-head-chips-host"))).toHaveLength(1);
});

test("flag ON, a task with no session yet keeps the panel's own menu (Delete of a never-run message)", async () => {
  publishTaskHeadUnified({ enabled: true });
  const r = await open([task({ key: "pending:e1", session_id: undefined, status: "upcoming" } as Partial<Task>)]);
  expect(hostAll(r, (n) => hasClass(n, "task-head"))).toHaveLength(1);
  // The panel's own menu behind it, under the chat ⋮'s words.
  const trigger = byLabel(r, "More options")!;
  expect(hasClass(trigger, "task-side-peek-kebab")).toBe(true);
  expect(hostAll(r, (n) => hasClass(n, "c-kebab"))).toHaveLength(0);
  expect(byLabel(r, "More actions")).toBeUndefined();
});

test("flag OFF: the head this panel has always drawn", async () => {
  const r = await open([task()]);
  expect(hostAll(r, (n) => hasClass(n, "task-side-peek-head"))).toHaveLength(1);
  expect(hostAll(r, (n) => hasClass(n, "task-head"))).toHaveLength(0);
  expect(byLabel(r, "More actions")).toBeDefined();
  expect(byLabel(r, "More options")).toBeUndefined();
});
