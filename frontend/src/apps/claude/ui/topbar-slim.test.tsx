// THE SLIM LINE (`slim`): a peek's chat header keeps the session label, the
// worktree seat and the host's extra seat, and drops the identity block.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

const { Topbar } = await import("./Topbar");

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
});

function render(over: Record<string, unknown> = {}) {
  let r!: ReactTestRenderer;
  act(() => {
    r = create(
      createElement(Topbar, {
        sessionId: "abcdef1234567890",
        subtitle: "alpha",
        running: false,
        slim: true,
        worktree: createElement("div", { className: "chat-worktree-menu" }, "main"),
        worktreeHint: createElement("span", { className: "hint-seat" }, "moved"),
        extra: createElement("a", { className: "extra-seat" }, "PR #7"),
        task: { key: "k", task_id: "TASK-001", title: "t", project: "/p", session_id: "s", status: "done" },
        ...over,
      } as never),
    );
  });
  mounted.push(r);
  return r;
}
const has = (r: ReactTestRenderer, cls: string) =>
  r.root.findAll((n) => typeof n.type === "string" && String(n.props.className ?? "").split(/\s+/).includes(cls)).length;

test("slim draws worktree, hint, session label and extra, and none of the identity", () => {
  const r = render();
  expect(has(r, "c-topbar-slim")).toBe(1);
  expect(has(r, "chat-worktree-menu")).toBe(1);
  expect(has(r, "hint-seat")).toBe(1);
  expect(has(r, "extra-seat")).toBe(1);
  expect(has(r, "c-session")).toBe(1);
  expect(has(r, "c-tb-title")).toBe(0);
  expect(has(r, "c-tb-file")).toBe(0);
  expect(has(r, "task-side-peek-who")).toBe(0);
});

test("slim still says running", () => {
  expect(has(render({ running: true }), "c-tb-run")).toBe(1);
});

test("without slim the title and file are drawn", () => {
  const r = render({ slim: false, task: null });
  expect(has(r, "c-topbar-slim")).toBe(0);
  expect(has(r, "c-tb-title")).toBe(1);
});
