// The worktree hint chip and the Topbar seat it rides in.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, describe, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";

import type { Task } from "@platform/lib/api";
import type { WorktreeHint } from "../protocol/types";

const { WorktreeHintChip } = await import("./WorktreeHintChip");
const { Topbar } = await import("./Topbar");

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
});
function mount(el: React.ReactElement): ReactTestRenderer {
  let r!: ReactTestRenderer;
  act(() => {
    r = create(el);
  });
  mounted.push(r);
  return r;
}
const text = (n: ReactTestInstance): string => n.children.map((c) => (typeof c === "string" ? c : text(c))).join("");
const chip = (r: ReactTestRenderer) =>
  r.root.findAll((n) => typeof n.type === "string" && String(n.props.className ?? "").includes("chat-worktree-hint"));

const bash: WorktreeHint = { path: ".claude/worktrees/foo", branch: "feat/foo", via: "bash" };

describe("WorktreeHintChip", () => {
  test("nothing for a null hint", () => {
    expect(chip(mount(<WorktreeHintChip hint={null} />)).length).toBe(0);
  });
  test("nothing when the chat is already in that folder", () => {
    const abs = { path: "/r/.claude/worktrees/foo/", branch: "feat/foo", via: "bash" as const };
    expect(chip(mount(<WorktreeHintChip hint={abs} currentPath="/r/.claude/worktrees/foo" />)).length).toBe(0);
    expect(chip(mount(<WorktreeHintChip hint={abs} currentPath="/r" />)).length).toBe(1);
  });
  test("a bash hint reads worktree · branch, with path and how in the tooltip", () => {
    const c = chip(mount(<WorktreeHintChip hint={bash} currentPath="/r" />))[0]!;
    expect(text(c)).toBe("worktree · feat/foo");
    expect(c.props.title).toContain(".claude/worktrees/foo");
    expect(c.props.title).toContain("feat/foo");
    expect(c.props.title).toContain("created with git worktree add");
  });
  test("falls back to the folder name, then the label, then 'subagent'", () => {
    const t = (h: WorktreeHint) => text(chip(mount(<WorktreeHintChip hint={h} />))[0]!);
    expect(t({ path: "../p/", branch: "", via: "enter" })).toBe("worktree · p");
    expect(t({ path: "", branch: "", via: "agent", label: "Fix it" })).toBe("worktree · Fix it");
    expect(t({ path: "", branch: "", via: "agent" })).toBe("worktree · subagent");
    const c = chip(mount(<WorktreeHintChip hint={{ path: "", branch: "", via: "agent" }} />))[0]!;
    expect(c.props.title).toContain("a subagent's isolated worktree");
  });
});

describe("Topbar worktreeHint seat", () => {
  const task = { key: "k", task_id: "TASK-1", title: "t", project: "/r", target: "/r/x", session_id: "s", status: "done" } as Task;
  const marker = createElement("i", { className: "seat-marker" });
  const seen = (props: object) =>
    mount(createElement(Topbar, { sessionId: "", subtitle: "a", running: false, worktreeHint: marker, ...props } as never))
      .root.findAll((n) => n.props.className === "seat-marker").length;
  test("rendered in the task, pending and plain layouts", () => {
    expect(seen({ task })).toBe(1);
    expect(seen({ pending: true })).toBe(1);
    expect(seen({})).toBe(1);
  });
});
