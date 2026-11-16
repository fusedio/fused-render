// THE ICON IS NOT THE STAR'S (owner, 2026-11-16): the ring of ANY numbered
// task opens the icon picker, a face picked by hand is drawn with the
// favourites switch off too, one that came with the star is not, and the
// picker's reset button reads "Remove" and stands only while a face is drawn.
import { installDomShim, installPortalContainer, removePortalContainer } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { createElement, type ReactElement } from "react";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";
import type { Task } from "@platform/lib/api";

const icons = await import("@platform/lib/task-icons-flag");
const favs = await import("@platform/lib/favourites-flag");
const { TaskIconPick } = await import("./TaskIconPick");
const { StatusIcon } = await import("./ScheduleTaskViews");
const { taskFaceShown } = await import("./task-icons");
const IconPicker = (await import("@platform/ui/IconPicker")).default;

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  icons.resetTaskIconsFlagForTests();
  favs.resetTaskFavouritesFlagForTests();
});

function switches(iconsOn: boolean, favsOn: boolean) {
  act(() => {
    icons.publishTaskIcons({ enabled: iconsOn });
    favs.publishTaskFavourites({ enabled: favsOn });
    favs.publishFavouritesParent({ enabled: favsOn });
  });
}

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
  ({ key: "sess-1", task_id: "TASK-042", icon_slot: null, icon_picked: false, ...over }) as Task;

const ring = () => createElement("span", { className: "ring" });
const pick = (t: Task) => createElement(TaskIconPick, { task: t, children: ring() });

function pickButtons(r: ReactTestRenderer): ReactTestInstance[] {
  return r.root.findAll((n) => n.type === "button" && n.props.className === "task-icon-pick");
}

test("the ring of any numbered task is the picker's button — with the experiment on, favourites switch or not", () => {
  for (const favsOn of [true, false]) {
    switches(true, favsOn);
    // A plain ring (never given a face, not starred).
    expect(pickButtons(render(pick(task())))).toHaveLength(1);
    // A face, picked or starred.
    expect(
      pickButtons(render(pick(task({ icon_slot: 3, icon_picked: true })))),
    ).toHaveLength(1);
  }
});

test("no button with the experiment off, or on a task with no number yet", () => {
  switches(false, true);
  const off = render(pick(task({ icon_slot: 3, icon_picked: true })));
  expect(pickButtons(off)).toHaveLength(0);
  expect(off.root.findAll((n) => n.props.className === "ring")).toHaveLength(1);
  switches(true, true);
  expect(pickButtons(render(pick(task({ task_id: "" }))))).toHaveLength(0);
});

test("a face is drawn when picked by hand, or when it came with the star and favourites are on", () => {
  // slot, picked, iconsOn, favsOn
  expect(taskFaceShown(3, true, true, false)).toBe(true);
  expect(taskFaceShown(3, false, true, false)).toBe(false);
  expect(taskFaceShown(3, false, true, true)).toBe(true);
  expect(taskFaceShown(3, true, false, true)).toBe(false);
  expect(taskFaceShown(null, true, true, true)).toBe(false);
  expect(taskFaceShown(999, true, true, true)).toBe(false);
});

test("StatusIcon draws a picked face with the favourites switch off, and the ring for a star's", () => {
  switches(true, false);
  const glyphOf = (r: ReactTestRenderer) => r.root.findAll((n) => n.type === "svg").length;
  expect(glyphOf(render(createElement(StatusIcon, { status: "done", icon: 3, iconPicked: true })))).toBe(1);
  expect(glyphOf(render(createElement(StatusIcon, { status: "done", icon: 3, iconPicked: false })))).toBe(0);
  switches(true, true);
  expect(glyphOf(render(createElement(StatusIcon, { status: "done", icon: 3 })))).toBe(1);
  expect(glyphOf(render(createElement(StatusIcon, { status: "done", icon: null, iconPicked: true })))).toBe(0);
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
  switches(true, true);
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
  // A plain ring has nothing to remove.
  expect(removeOf(task())).toBeUndefined();
  // A face — picked, or the star's — can be removed.
  expect(typeof removeOf(task({ icon_slot: 3, icon_picked: true }))).toBe("function");
  expect(typeof removeOf(task({ icon_slot: 3, icon_picked: false }))).toBe("function");
  // The favourites switch off: the star's face is not drawn, so no Remove;
  // a picked one still is.
  switches(true, false);
  expect(removeOf(task({ icon_slot: 3, icon_picked: false }))).toBeUndefined();
  expect(typeof removeOf(task({ icon_slot: 3, icon_picked: true }))).toBe("function");
}
