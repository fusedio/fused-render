import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { expect, test } from "bun:test";
import { createElement } from "react";
import { act, create } from "react-test-renderer";
import type { Task } from "@platform/lib/api";

const { TaskPrChip } = await import("./TaskPrChip");

const task = (over: Partial<Task>): Task => ({ key: "k", title: "", project: "/p", ...over }) as Task;

function render(t: Task) {
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(createElement(TaskPrChip, { task: t }));
  });
  return r;
}

test("a task whose title names a PR gets a link chip to GitHub", () => {
  const r = render(task({ title: "Fix thing (PR #123)", repo: "fused-io/fused-render" } as Partial<Task>));
  const a = r.root.findByType("a");
  expect(a.props.href).toBe("https://github.com/fused-io/fused-render/pull/123");
  expect(a.props.target).toBe("_blank");
  expect(JSON.stringify(r.toJSON())).toContain("123");
});

test("a pull-request link in the title carries its own repo", () => {
  const r = render(task({ title: "see https://github.com/a/b/pull/9" }));
  expect(r.root.findByType("a").props.href).toBe("https://github.com/a/b/pull/9");
});

test("a task with no PR renders nothing", () => {
  expect(render(task({ title: "plain task" })).toJSON()).toBeNull();
});
