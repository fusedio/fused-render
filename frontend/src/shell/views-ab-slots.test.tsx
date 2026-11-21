// THE LIST ROW'S TWO, ON EVERY TASK VIEW (owner, 2026-11-16: "In card view we
// are missing the archive button thing on task id, and click to change icon in
// status ring" — "Also detailed view doesn't have archive button on task id and
// status thing"). The Board card, the Cards wall tile and the Detailed view's
// card each draw:
//   1. THE ID SLOT — TASK-nnn with the filing button over it (`.tasks-idslot`),
//      no button where the filing cannot change, and no second Archive in the
//      view's own hover strip;
//   2. THE RING AS A BUTTON — the icon picker's (TaskIconPick), icons
//      experiment on, whose press stops there.
import { installDomShim, installPortalContainer, removePortalContainer } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, expect, mock, test } from "bun:test";
import { createElement, type ReactElement } from "react";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";
import { dropSharedPrefsRead, type Task } from "@platform/lib/api";

const icons = await import("@platform/lib/task-icons-flag");
const { TaskBoard } = await import("./ScheduleTaskViews");
const { TaskCards } = await import("./TaskCards");
const { TaskWorkspace } = await import("./TaskWorkspace");

const realFetch = globalThis.fetch;
const mounted: ReactTestRenderer[] = [];
const store: Record<string, string> = {};
let posts: { url: string; body: unknown }[] = [];
// The prefs a mounting view reads: the switch's own answer, so the flag's
// first read lands where `switches` put it.
let prefs: Record<string, unknown> = {};

beforeEach(() => {
  dropSharedPrefsRead();
  for (const k of Object.keys(store)) delete store[k];
  // The Detailed view folds "Safe to archive" by default; these cards sit there.
  store["fused.tasks.workspace.collapsed"] = JSON.stringify({ clear: false, archived: false });
  // The icon picker is portalled to the body; a portal is its own root, so
  // its refs come off the BODY's node mock (TaskIconPick.test's arrangement).
  installPortalContainer();
  (globalThis as unknown as { document: { body: { createNodeMock: unknown } } }).document.body.createNodeMock =
    nodeMock;
  (globalThis as { localStorage?: unknown }).localStorage = {
    getItem: (k: string) => store[k] ?? null,
    setItem: (k: string, v: string) => {
      store[k] = v;
    },
    removeItem: (k: string) => {
      delete store[k];
    },
  };
  posts = [];
  globalThis.fetch = mock(async (url: string, init?: RequestInit) => {
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    posts.push({ url, body });
    const reply: Record<string, unknown> = {
      "/api/prefs": prefs,
      "/api/tasks/ports": { ports: {} },
      "/api/tasks/holds": { holds: {} },
      "/api/tasks/archive": { ok: true, key: body?.key, cancelled: 0, filed: true },
      "/api/tasks/unarchive": { ok: true, key: body?.key, unfiled: true, status: "done" },
    };
    return new Response(JSON.stringify(reply[url] ?? {}), { status: 200 });
  }) as unknown as typeof fetch;
});
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  // Off by publish, not by reset: a reset drops the cached read, and the next
  // render would GET /api/prefs into a later test's fetch log.
  act(() => {
    icons.publishTaskIcons({ enabled: false });
  });
  removePortalContainer();
  delete (globalThis as { localStorage?: unknown }).localStorage;
  globalThis.fetch = realFetch;
});

function switches(iconsOn: boolean) {
  prefs = { task_icons: { enabled: iconsOn } };
  act(() => {
    icons.publishTaskIcons({ enabled: iconsOn });
  });
}

const nodeMock = () => ({
  focus() {},
  contains: () => false,
  querySelector: () => null,
  querySelectorAll: () => [],
  closest: () => null,
  getBoundingClientRect: () => ({ top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }),
  offsetWidth: 0,
  offsetHeight: 0,
  addEventListener() {},
  removeEventListener() {},
  style: {},
});

async function render(el: ReactElement): Promise<ReactTestRenderer> {
  let r!: ReactTestRenderer;
  await act(async () => {
    r = create(el, { createNodeMock: nodeMock });
  });
  mounted.push(r);
  return r;
}

const task = (over: Partial<Task>): Task =>
  ({
    key: "k", task_id: "TASK-012", title: "Fix it", project: "/p", target: "/p", session_id: "s",
    status: "done", unread: 0, started: 1, icon_slot: null, icon_picked: false, ...over,
  }) as Task;

const cls = (n: ReactTestInstance) => String(n.props.className ?? "");
const hosts = (r: ReactTestRenderer, pred: (n: ReactTestInstance) => boolean) =>
  r.root.findAll((n) => typeof n.type === "string" && pred(n));
const byClass = (r: ReactTestRenderer | ReactTestInstance, c: string) =>
  ("root" in r ? r.root : r).findAll(
    (n) => typeof n.type === "string" && cls(n).split(" ").includes(c),
  );
const text = (n: ReactTestInstance): string =>
  n.children.map((c) => (typeof c === "string" ? c : text(c))).join("");
const fileActs = (n: ReactTestRenderer | ReactTestInstance) =>
  byClass(n, "tasks-act").filter((b) => /tasks-act--(archive|unarchive)/.test(cls(b)));
const pickers = (r: ReactTestRenderer) => byClass(r, "task-icon-pick");

const evt = () => {
  const seen = { stopped: false, prevented: false };
  return {
    seen,
    e: {
      stopPropagation: () => (seen.stopped = true),
      preventDefault: () => (seen.prevented = true),
      currentTarget: nodeMock(),
      key: "Enter",
    },
  };
};

const board = (tasks: Task[]) =>
  render(createElement(TaskBoard, { tasks, onReload: () => {} }));
const wall = (tasks: Task[]) => render(createElement(TaskCards, { tasks, onReload: () => {} }));
const detailed = (tasks: Task[]) =>
  render(
    createElement(TaskWorkspace, {
      tasks, allTasks: tasks, emptyLabel: "Nothing.", peekOn: false, onReload: () => {}, onNewTask: () => {},
    }),
  );

// ---- the Board card ---------------------------------------------------------

test("BOARD a/b: the id slot holds TASK-nnn and the filing button, and the hover strip drops its Archive", async () => {
  switches(false);
  const r = await board([task({ key: "a", status: "done" })]);
  const [slot] = byClass(r, "tasks-idslot");
  expect(slot).toBeDefined();
  expect(text(byClass(slot!, "tasks-id")[0]!)).toBe("T012");
  const [btn] = fileActs(slot!);
  // A span-button: the card is a <button>, and a button cannot hold one.
  expect(btn!.type).toBe("span");
  expect(btn!.props.role).toBe("button");
  expect(btn!.props.tabIndex).toBe(0);
  expect(cls(btn!)).toBe("tasks-act tasks-act--archive");
  // One filing button per card.
  expect(fileActs(r)).toHaveLength(1);
  expect(byClass(r, "tasks-card-wrap--ab")).toHaveLength(1);
  // Its press is its own: no bubbling into the card, no default, and its
  // mousedown neither lifts the card nor focuses it.
  const down = evt();
  btn!.props.onMouseDown(down.e);
  expect(down.seen).toEqual({ stopped: true, prevented: true });
  const press = evt();
  await act(async () => {
    btn!.props.onClick(press.e);
  });
  expect(press.seen).toEqual({ stopped: true, prevented: true });
  expect(posts.some((p) => p.url === "/api/tasks/archive" && (p.body as { key: string }).key === "a")).toBe(true);
});

test("BOARD a/b: no filing button while a run is in flight — the number stays", async () => {
  switches(false);
  const r = await board([task({ key: "a", status: "in_progress" })]);
  const [slot] = byClass(r, "tasks-idslot");
  expect(text(slot!)).toBe("T012");
  expect(fileActs(r)).toHaveLength(0);
});

test("BOARD a/b, icons on: a drawn ring is the picker's span-button, and its press stops there", async () => {
  switches(true);
  const r = await board([task({ key: "a", status: "needs_attention" })]);
  const [pick] = pickers(r);
  expect(pick!.type).toBe("span");
  expect(pick!.props.role).toBe("button");
  expect(pick!.props.tabIndex).toBe(0);
  expect(pick!.props["aria-label"]).toBe("Choose icon");
  const down = evt();
  pick!.props.onMouseDown(down.e);
  expect(down.seen).toEqual({ stopped: true, prevented: true });
  const press = evt();
  act(() => pick!.props.onClick(press.e));
  expect(press.seen).toEqual({ stopped: true, prevented: true });
  expect(pickers(r)[0]!.props["aria-expanded"]).toBe(true);
});

// ---- the Cards wall ---------------------------------------------------------

test("CARDS a/b: the id slot holds TASK-nnn and the filing button, and the doors drop their Archive", async () => {
  switches(false);
  const r = await wall([task({ key: "a", status: "done" })]);
  const [slot] = byClass(r, "tasks-idslot");
  expect(text(byClass(slot!, "tasks-id")[0]!)).toBe("T012");
  const [btn] = fileActs(slot!);
  expect(btn!.type).toBe("button");
  expect(cls(btn!)).toBe("tasks-act tasks-act--archive");
  expect(byClass(r, "task-card--ab")).toHaveLength(1);
  // No filing door left in the strip: one Archive per tile.
  const doors = byClass(r, "task-card-door");
  expect(doors.some((d) => d.props["aria-label"] === "Archive")).toBe(false);
  const press = evt();
  await act(async () => {
    btn!.props.onClick(press.e);
  });
  expect(press.seen.stopped).toBe(true);
  expect(posts.some((p) => p.url === "/api/tasks/archive" && (p.body as { key: string }).key === "a")).toBe(true);
});

test("CARDS a/b: no filing button while a run is in flight", async () => {
  switches(false);
  const r = await wall([task({ key: "a", status: "in_progress" })]);
  expect(text(byClass(r, "tasks-idslot")[0]!)).toBe("T012");
  expect(fileActs(r)).toHaveLength(0);
});

test("CARDS a/b, icons on: the ring is the picker's button, and its press does not open the popup", async () => {
  switches(true);
  const r = await wall([task({ key: "a", status: "done" })]);
  const [pick] = hosts(r, (n) => n.type === "button" && cls(n) === "task-icon-pick");
  expect(pick).toBeDefined();
  const press = evt();
  act(() => pick!.props.onClick(press.e));
  expect(press.seen.stopped).toBe(true);
  expect(pickers(r)[0]!.props["aria-expanded"]).toBe(true);
});

// ---- the Detailed view ------------------------------------------------------

test("DETAILED a/b: Archive leaves the ring's slot for the id's, and archives with Undo", async () => {
  switches(false);
  const r = await detailed([task({ key: "a", status: "done" })]);
  const [mark] = byClass(r, "ws-mark");
  expect(fileActs(mark!)).toHaveLength(0);
  const [slot] = byClass(r, "tasks-idslot");
  expect(text(byClass(slot!, "tasks-id")[0]!)).toBe("T012");
  const [btn] = fileActs(slot!);
  expect(cls(btn!)).toBe("tasks-act tasks-act--archive");
  expect(fileActs(r)).toHaveLength(1);
  expect(byClass(r, "ws-card--ab")).toHaveLength(1);
  await act(async () => {
    btn!.props.onClick(evt().e);
  });
  expect(posts.some((p) => p.url === "/api/tasks/archive" && (p.body as { key: string }).key === "a")).toBe(true);
  expect(r.root.findAllByProps({ className: "ws-undo-btn" })).toHaveLength(1);
});

test("DETAILED a/b: no filing button while a run is in flight", async () => {
  switches(false);
  const r = await detailed([task({ key: "a", status: "in_progress" })]);
  expect(text(byClass(r, "tasks-idslot")[0]!)).toBe("T012");
  expect(fileActs(r)).toHaveLength(0);
});

test("DETAILED a/b, icons on: the ring is the picker's button, and its press neither selects nor opens", async () => {
  switches(true);
  const r = await detailed([task({ key: "a", status: "done" })]);
  const [mark] = byClass(r, "ws-mark");
  const [pick] = mark!.findAll((n) => n.type === "button" && cls(n) === "task-icon-pick");
  expect(pick).toBeDefined();
  const press = evt();
  act(() => pick!.props.onClick(press.e));
  expect(press.seen.stopped).toBe(true);
  expect(byClass(r, "ws-card")[0]!.props["aria-selected"]).toBe(false);
});

// ---- a click from the picker's portal is not the card's (Bugbot, PR #2300) ---
// The picker is portalled to the body, and React bubbles its clicks up the
// component tree into the card that holds the ring. Each host opens only on a
// press inside its own DOM.

const fromPortal = () => ({ currentTarget: { contains: () => false }, target: {}, stopPropagation() {} });
const fromCard = () => ({ currentTarget: { contains: () => true }, target: {}, stopPropagation() {} });

test("BOARD: a click bubbled from a portal does not open the card; one on the card does", async () => {
  switches(true);
  const opened: string[] = [];
  const r = await render(
    createElement(TaskBoard, {
      tasks: [task({ key: "d", task_id: "", session_id: "", kind: "draft" } as Partial<Task>)],
      onReload: () => {},
      onOpenDraft: (t: Task) => opened.push(t.key),
    }),
  );
  const [card] = hosts(r, (n) => n.type === "button" && cls(n).split(" ").includes("schedule-tv-card"));
  act(() => card!.props.onClick(fromPortal()));
  expect(opened).toEqual([]);
  act(() => card!.props.onClick(fromCard()));
  expect(opened).toEqual(["d"]);
});

test("DETAILED: a click bubbled from a portal neither selects nor opens the card", async () => {
  switches(true);
  const r = await detailed([task({ key: "a", status: "done" })]);
  const card = () => byClass(r, "ws-card")[0]!;
  act(() => card().props.onClick(fromPortal()));
  expect(card().props["aria-selected"]).toBe(false);
  act(() => card().props.onClick(fromCard()));
  expect(card().props["aria-selected"]).toBe(true);
});
