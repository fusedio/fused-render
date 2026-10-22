import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { expect, test } from "bun:test";
import { createElement } from "react";
import { act, create } from "react-test-renderer";
import type { Task } from "@platform/lib/api";

const { TaskPrChip } = await import("./TaskPrChip");
const { PrPopList } = await import("@platform/ui/TaskPrChip");
const { requestPrStatuses, resetPrStatusForTests } = await import("@platform/lib/prStatus");

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

const three = () =>
  task({
    prs: [
      { repo: "a/b", number: 1, url: "https://github.com/a/b/pull/1" },
      { repo: "a/b", number: 2, url: "https://github.com/a/b/pull/2" },
      { repo: "c/d", number: 3, url: "https://github.com/c/d/pull/3" },
    ],
  });

test("several PRs: the newest is the link, the rest sit behind +N", () => {
  const r = render(three());
  const link = r.root.findByProps({ className: "task-pr-chip" });
  expect(link.props.href).toBe("https://github.com/c/d/pull/3");
  const more = r.root.findByProps({ className: "task-pr-more" });
  expect(more.props["aria-label"]).toBe("2 more pull requests");
  expect(JSON.stringify(more.children)).toContain("+");
});

test("one PR has no +N button", () => {
  const r = render(task({ prs: [{ repo: "a/b", number: 1, url: "u" }] }));
  expect(r.root.findAllByProps({ className: "task-pr-more" })).toHaveLength(0);
});

test("the popover list names the others, with a status dot only when known", async () => {
  resetPrStatusForTests();
  const realFetch = globalThis.fetch;
  globalThis.fetch = (async () =>
    ({ ok: true, status: 200, json: async () => ({ "a/b#2": "merged" }) }) as Response) as unknown as typeof fetch;
  try {
    const refs = [
      { repo: "a/b", number: 2 },
      { repo: "a/b", number: 1 },
    ];
    await requestPrStatuses(refs.map((r) => `${r.repo}#${r.number}`));
    let r!: ReturnType<typeof create>;
    act(() => {
      r = create(createElement(PrPopList, { refs }));
    });
    const links = r.root.findAllByType("a");
    expect(links.map((a) => a.props.href)).toEqual(["https://github.com/a/b/pull/2", "https://github.com/a/b/pull/1"]);
    const dots = r.root.findAllByProps({ className: "task-pr-dot" });
    expect(dots).toHaveLength(1);
    expect(dots[0].props["data-status"]).toBe("merged");
  } finally {
    globalThis.fetch = realFetch;
    resetPrStatusForTests();
  }
});
