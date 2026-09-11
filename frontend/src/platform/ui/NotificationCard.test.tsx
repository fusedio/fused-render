// The shared row's own contract, independent of any of its six callers:
// every optional part renders (or doesn't) exactly off its own prop, and the
// two action families stay on their own classes.
import { describe, expect, it, test } from "bun:test";
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

// `.dl-status.with-glyph` is a flex row (glyph + text on one line), which
// would collide with `-webkit-line-clamp`'s own `-webkit-box` display
// requirement if the clamp sat on the same element. The status text lives
// in its own `.dl-status-text` span so the clamp has a `-webkit-box`
// element to apply to, independent of the flex row around it.
test("terminal status wraps its text in its own clamp span, separate from the flex row", () => {
  const tree = render({ title: "a", status: "a long failure message", terminal: "error" });
  const clampSpans = findAll(tree, "dl-status-text");
  expect(clampSpans).toHaveLength(1);
  expect(clampSpans[0].children).toEqual(["a long failure message"]);
});

// Code review finding (PR #1104): `terminal` alone, with no `status` text,
// used to render NOTHING — the glyph only ever appeared inside the
// `status != null` block, so a caller that only has a tone (not a status
// string to go with it — MessagePopupCard.tsx, RepoUpdatesDock.tsx's
// MessageRowView) got an error row that looked identical to an info one.
test("terminal renders its glyph even when there is no status text to go with it (finding #3)", () => {
  const tree = render({ title: "a", terminal: "error" });
  expect(findAll(tree, "dl-status")).toHaveLength(1);
  // No text was given — the status line carries the glyph alone, no empty
  // .dl-status-text span rendered beside it.
  expect(findAll(tree, "dl-status-text")).toHaveLength(0);

  const noTerminal = render({ title: "a" });
  expect(findAll(noTerminal, "dl-status")).toHaveLength(0);
});

test("an explicit `role` sets the row's aria role, distinct from rowClick's own implicit button role (finding #3)", () => {
  const alert = render({ title: "a", role: "alert" });
  expect(findAll(alert, "dl-row")[0].props.role).toBe("alert");

  const status = render({ title: "a", role: "status" });
  expect(findAll(status, "dl-row")[0].props.role).toBe("status");

  const none = render({ title: "a" });
  expect(findAll(none, "dl-row")[0].props.role).toBeUndefined();
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

describe("notifications.css: with-glyph keeps the line-clamp alive on its text span", () => {
  const { readFileSync } = require("node:fs") as typeof import("node:fs");
  const { join } = require("node:path") as typeof import("node:path");
  const CSS = readFileSync(join(import.meta.dir, "../../styles/notifications.css"), "utf8");

  function block(css: string, selector: string): string {
    const at = css.indexOf(selector + " {");
    expect(at).toBeGreaterThan(-1);
    return css.slice(at, css.indexOf("}", at));
  }

  it("with-glyph itself is the flex row and carries no clamp declaration", () => {
    const withGlyph = block(CSS, ".dl-status.with-glyph");
    expect(withGlyph).toContain("display: flex;");
    expect(withGlyph).not.toContain("-webkit-line-clamp");
  });

  it("the inner text span is the -webkit-box clamp target", () => {
    const textSpan = block(CSS, ".dl-status.with-glyph .dl-status-text");
    expect(textSpan).toContain("display: -webkit-box;");
    expect(textSpan).toContain("-webkit-line-clamp: 3;");
  });
});

test("onDismiss's disabled prop reaches the ✕ button, so a request in flight can lock it", () => {
  const idle = render({ title: "a", onDismiss: { onClick: () => {} } });
  expect(findAll(idle, "dl-x")[0].props.disabled).toBeFalsy();

  const busy = render({ title: "a", onDismiss: { onClick: () => {}, disabled: true } });
  expect(findAll(busy, "dl-x")[0].props.disabled).toBe(true);
});
