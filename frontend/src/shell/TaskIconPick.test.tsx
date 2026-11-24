// THE ICON IS NOT THE STAR'S (owner, 2026-11-16): the ring of ANY numbered
// task opens the icon picker, and the picker's reset button reads "Remove"
// and stands only while a face is drawn.
import { installDomShim, installPortalContainer, removePortalContainer } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { createElement, type ReactElement } from "react";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";
import type { Task } from "@platform/lib/api";

const { TaskIconPick } = await import("./TaskIconPick");
const { StatusIcon } = await import("./ScheduleTaskViews");
const { taskFaceShown } = await import("./task-icons");
const IconPicker = (await import("@platform/ui/IconPicker")).default;

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
});

// A node stand-in rich enough for the picker's own effects (focus, the
// filter box lookup, its on-screen placement).
const nodeMock = (el: ReactElement) => ({
  tagName: typeof el.type === "string" ? el.type.toUpperCase() : "DIV",
  focus() {},
  contains: () => false,
  querySelector: () => null,
  getBoundingClientRect: () => ({ top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }),
  offsetWidth: 0,
  offsetHeight: 0,
  style: {},
});

function render(el: ReactElement): ReactTestRenderer {
  let r!: ReactTestRenderer;
  act(() => {
    r = create(el, { createNodeMock: nodeMock });
  });
  mounted.push(r);
  return r;
}

const task = (over: Partial<Task> = {}) =>
  ({ key: "sess-1", task_id: "TASK-042", icon_slot: null, ...over }) as Task;

const ring = () => createElement("span", { className: "ring" });
const pick = (t: Task) => createElement(TaskIconPick, { task: t, children: ring() });

function pickButtons(r: ReactTestRenderer): ReactTestInstance[] {
  return r.root.findAll((n) => n.type === "button" && n.props.className === "task-icon-pick");
}

test("the ring of any numbered task is the picker's button", () => {
  // A plain ring (never given a face, not starred).
  expect(pickButtons(render(pick(task())))).toHaveLength(1);
  // A face, picked or starred.
  expect(pickButtons(render(pick(task({ icon_slot: 3 }))))).toHaveLength(1);
});

test("the button keeps the ring's status as its description", () => {
  const r = render(
    createElement(TaskIconPick, {
      task: task(),
      children: createElement(StatusIcon, { status: "done", unread: true }),
    }),
  );
  const btn = pickButtons(r)[0]!;
  expect(btn.props["aria-label"]).toBe("Choose icon");
  const id = btn.props["aria-describedby"];
  expect(typeof id).toBe("string");
  const face = r.root.find((n) => n.props.id === id);
  // The ring inside it carries the words a screen reader is described with.
  expect(face.findAll((n) => n.type === "span" && /^Done/.test(String(n.props["aria-label"] ?? "")))).toHaveLength(1);
});

test("no button on a task with no number yet: the ring as it is", () => {
  const none = render(pick(task({ task_id: "", icon_slot: 3 })));
  expect(pickButtons(none)).toHaveLength(0);
  expect(none.root.findAll((n) => n.props.className === "ring")).toHaveLength(1);
});

test("a face is drawn for a known slot", () => {
  expect(taskFaceShown(3)).toBe(true);
  expect(taskFaceShown(null)).toBe(false);
  expect(taskFaceShown(999)).toBe(false);
});

test("StatusIcon draws the face for a slot, and the ring for none", () => {
  const glyphOf = (r: ReactTestRenderer) => r.root.findAll((n) => n.type === "svg").length;
  expect(glyphOf(render(createElement(StatusIcon, { status: "done", icon: 3 })))).toBe(1);
  expect(glyphOf(render(createElement(StatusIcon, { status: "done", icon: null })))).toBe(0);
});

test("the picker's reset button is a host's to give: shown with `onRemove`, absent without", () => {
  const set = {
    label: "Icons",
    recent: "task-test",
    cells: [{ id: "cat", title: "cat" }],
    render: () => null,
    onPick: () => {},
    removeLabel: "Remove",
    removeTitle: "Remove the icon — show the status ring",
  };
  const removed: string[] = [];
  const words = (r: ReactTestRenderer) =>
    r.root.findAll((n) => n.type === "button" && n.props.title === set.removeTitle).length;
  const withRemove = render(
    createElement(IconPicker, {
      anchor: { top: 0, left: 0 },
      set,
      onRemove: () => removed.push("x"),
      onClose: () => {},
    }),
  );
  expect(words(withRemove)).toBe(1);
  const btn = withRemove.root.find((n) => n.type === "button" && n.props.title === set.removeTitle);
  expect(JSON.stringify(withRemove.toJSON())).toContain("Remove");
  act(() => btn.props.onClick());
  expect(removed).toEqual(["x"]);
  const without = render(createElement(IconPicker, { anchor: { top: 0, left: 0 }, set, onClose: () => {} }));
  expect(words(without)).toBe(0);
});

test("pressed, the button opens the picker — with Remove only while a face is drawn", () => {
  // The picker is portalled to the body (TaskIconPick's own note).
  installPortalContainer();
  // A portal is its own root: its refs come off the BODY's node mock, which
  // must answer the picker's placement too.
  (globalThis as unknown as { document: { body: { createNodeMock: unknown } } }).document.body.createNodeMock = nodeMock;
  try {
    pressedPickers();
  } finally {
    for (const r of mounted.splice(0)) act(() => r.unmount());
    removePortalContainer();
  }
});

function pressedPickers() {
  const removeOf = (t: Task) => {
    const r = render(pick(t));
    const btn = pickButtons(r)[0]!;
    act(() =>
      btn.props.onClick({
        stopPropagation() {},
        currentTarget: { getBoundingClientRect: () => ({ top: 10, left: 10 }) },
      }),
    );
    expect(pickButtons(r)[0]!.props["aria-expanded"]).toBe(true);
    const picker = r.root.findByType(IconPicker);
    expect(picker.props.set.removeLabel).toBe("Remove");
    return picker.props.onRemove;
  };
  // A PRESS OR A KEY IN THE PICKER STOPS AT THE PICKER (2026-11-16): React
  // bubbles a portal's events to the button's ancestors, and a Recent-chats
  // row opens its chat on its own click and Enter.
  {
    const r = render(pick(task()));
    act(() =>
      pickButtons(r)[0]!.props.onClick({
        stopPropagation() {},
        currentTarget: { getBoundingClientRect: () => ({ top: 10, left: 10 }) },
      }),
    );
    const shell = r.root.findByType(IconPicker).parent!;
    for (const ev of ["onClick", "onDoubleClick", "onMouseDown", "onKeyDown", "onKeyUp"]) {
      let stopped = 0;
      shell.props[ev]({ stopPropagation: () => (stopped += 1) });
      expect(stopped).toBe(1);
    }
    // Bubble phase only: a rail card's capture-phase watchers still hear it.
    expect(Object.keys(shell.props).some((k) => k.endsWith("Capture"))).toBe(false);
  }
  // A plain ring has nothing to remove.
  expect(removeOf(task())).toBeUndefined();
  // A face — picked, or the star's — can be removed.
  expect(typeof removeOf(task({ icon_slot: 3 }))).toBe("function");
}
