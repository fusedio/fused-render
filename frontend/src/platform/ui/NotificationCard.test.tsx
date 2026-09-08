// The shared row's own contract, independent of any of its six callers:
// every optional part renders (or doesn't) exactly off its own prop, and the
// two action families stay on their own classes.
import { expect, test } from "bun:test";
import { create } from "react-test-renderer";
import type { ReactTestRendererJSON } from "react-test-renderer";

import NotificationCard from "@platform/ui/NotificationCard";

function findAll(node: ReactTestRendererJSON | null, className: string): ReactTestRendererJSON[] {
  if (node === null || typeof node === "string") return [];
  const hits: ReactTestRendererJSON[] = [];
  if (
    typeof node.props?.className === "string" &&
    node.props.className.split(" ").includes(className)
  ) {
    hits.push(node);
  }
  for (const child of node.children ?? []) {
    if (typeof child !== "string") hits.push(...findAll(child, className));
  }
  return hits;
}

function render(props: Parameters<typeof NotificationCard>[0]) {
  return create(<NotificationCard {...props} />).toJSON() as ReactTestRendererJSON;
}

test("a bare title renders the row and head, nothing else optional", () => {
  const tree = render({ title: "hello" });
  expect(findAll(tree, "dl-row")).toHaveLength(1);
  expect(findAll(tree, "dl-row-head")).toHaveLength(1);
  expect(findAll(tree, "dl-model")).toHaveLength(0);
  expect(findAll(tree, "dl-row-figures")).toHaveLength(0);
  expect(findAll(tree, "dl-bar")).toHaveLength(0);
  expect(findAll(tree, "dl-status")).toHaveLength(0);
});

test("titleMode 'id' adds dl-title-id; default wraps without it", () => {
  const wrapped = render({ title: "a" });
  expect(findAll(wrapped, "dl-title-id")).toHaveLength(0);
  const id = render({ title: "a", titleMode: "id" });
  expect(findAll(id, "dl-title-id")).toHaveLength(1);
});

test("progress: undefined draws no bar, null draws indeterminate, a number fills", () => {
  expect(findAll(render({ title: "a" }), "dl-bar")).toHaveLength(0);
  const indet = render({ title: "a", progress: null });
  const fill = findAll(indet, "dl-bar-fill")[0];
  expect(fill.props["data-indeterminate"]).toBe("1");
  const half = render({ title: "a", progress: 0.5 });
  expect(findAll(half, "dl-bar-fill")[0].props.style.width).toBe("50%");
});

test("stalled dims the row and tones the bar", () => {
  const tree = render({ title: "a", progress: 0.2, stalled: true });
  expect(findAll(tree, "is-stalled")).toHaveLength(2); // .dl-row and .dl-bar
});

test("terminal renders the glyph beside the status text, on one line", () => {
  const tree = render({ title: "a", status: "4.6 GB", terminal: "done" });
  const line = findAll(tree, "dl-status")[0];
  expect((line.props.className as string).split(" ")).toContain("with-glyph");
  expect(findAll(tree, "dl-status")).toHaveLength(1);
});

test("liveAction and navAction render as distinct classes, never merged", () => {
  const tree = render({
    title: "a",
    liveAction: { label: "Unload", onClick: () => {} },
    navAction: { label: "Update", onClick: () => {} },
  });
  expect(findAll(tree, "dl-row-cancel")).toHaveLength(1);
  expect(findAll(tree, "q-all")).toHaveLength(1);
  expect(findAll(tree, "dl-row-cancel")[0]).not.toBe(findAll(tree, "q-all")[0]);
});

test("onDismiss renders the ✕", () => {
  const tree = render({ title: "a", onDismiss: { onClick: () => {} } });
  expect(findAll(tree, "dl-x")).toHaveLength(1);
});

test("rowClick makes the row a keyboard-reachable button-role div, not a <button>", () => {
  const tree = render({ title: "a", rowClick: { onClick: () => {} } });
  const row = findAll(tree, "dl-row")[0];
  expect(row.type).toBe("div");
  expect(row.props.role).toBe("button");
  expect(row.props.tabIndex).toBe(0);
  expect((row.props.className as string).split(" ")).toContain("dl-row-open");
});

// A waiting-task row combines `rowClick` (open the conversation) with
// `onDismiss` (the ✕) — Enter/Space bubbling up from the nested dismiss
// button must not also fire the row's own navigation.
test("rowClick's Enter/Space handler ignores a keydown that bubbled up from a nested control", () => {
  const rowClickSpy = { calls: 0 };
  const tree = render({
    title: "a",
    rowClick: { onClick: () => rowClickSpy.calls++ },
    onDismiss: { onClick: () => {} },
  });
  const row = findAll(tree, "dl-row")[0];
  const dismissButton = findAll(tree, "dl-x")[0];
  const onKeyDown = row.props.onKeyDown as (e: unknown) => void;

  // Simulates the keydown as it reaches the row's handler once it has
  // bubbled from the focused dismiss button: `target` is the button,
  // `currentTarget` is the row.
  const preventDefault = { called: false };
  onKeyDown({
    key: "Enter",
    target: dismissButton,
    currentTarget: row,
    preventDefault: () => {
      preventDefault.called = true;
    },
  });
  expect(rowClickSpy.calls).toBe(0);
  expect(preventDefault.called).toBe(false);

  // A keydown that targets the row itself (no nested control focused) still
  // activates it.
  onKeyDown({
    key: "Enter",
    target: row,
    currentTarget: row,
    preventDefault: () => {
      preventDefault.called = true;
    },
  });
  expect(rowClickSpy.calls).toBe(1);
  expect(preventDefault.called).toBe(true);
});
