// The worktree hint chip and the Topbar seat it rides in.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, describe, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";

import type { Task } from "@platform/lib/api";
import type { WorktreeHint } from "../protocol/types";

const { WorktreeHintChip, visibleWorktreeHint, shownWorktreeHint, isSameFolder, breakoutCwdFor } = await import("./WorktreeHintChip");
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
    expect(t({ path: "", branch: "", via: "enter" })).toBe("worktree");
    expect(t({ path: "", branch: "", via: "bash" })).toBe("worktree");
    const c = chip(mount(<WorktreeHintChip hint={{ path: "", branch: "", via: "agent" }} />))[0]!;
    expect(c.props.title).toContain("a subagent's isolated worktree");
  });
});

describe("isSameFolder", () => {
  test("a relative hint matches only by name or trailing path, never with ..", () => {
    expect(isSameFolder("../p", "/x/p")).toBe(false);
    expect(isSameFolder("./p", "/x/p")).toBe(true);
    expect(isSameFolder("p", "/x/p")).toBe(true);
    expect(isSameFolder("x/p", "/x/p")).toBe(true);
    expect(isSameFolder("a/p", "/x/p")).toBe(false);
    expect(isSameFolder("./p", "/x/q")).toBe(false);
    expect(isSameFolder("..", "/x/p")).toBe(false);
  });
  test("an absolute hint must be exactly equal", () => {
    expect(isSameFolder("/x/p", "/x/p")).toBe(true);
    expect(isSameFolder("/x/p", "/x/p/")).toBe(true);
    expect(isSameFolder("/y/p", "/x/p")).toBe(false);
  });
});

describe("visibleWorktreeHint", () => {
  const agent: WorktreeHint = { path: "", branch: "", via: "agent", label: "x" };
  const t = (status: string, isolation: string) => ({ id: "a", status, isolation }) as never;
  test("an agent hint shows only while an isolated task is live", () => {
    expect(visibleWorktreeHint(agent, [t("running", "worktree")])).toBe(agent);
    expect(visibleWorktreeHint(agent, [t("completed", "worktree")])).toBe(null);
    expect(visibleWorktreeHint(agent, [t("running", "")])).toBe(null);
    expect(visibleWorktreeHint(agent, undefined)).toBe(null);
  });
  test("a finished agent hint falls back to the main agent's worktree (prev)", () => {
    const prev: WorktreeHint = { path: "/r/.claude/worktrees/main-wt", branch: "", via: "enter" };
    const withPrev: WorktreeHint = { ...agent, prev };
    expect(visibleWorktreeHint(withPrev, [t("running", "worktree")])).toBe(withPrev);
    expect(visibleWorktreeHint(withPrev, [t("completed", "worktree")])).toBe(prev);
    expect(breakoutCwdFor(shownWorktreeHint(withPrev, [], "/r"))).toBe("/r/.claude/worktrees/main-wt");
  });
  test("other hints do not depend on tasks", () => {
    expect(visibleWorktreeHint(bash, [])).toBe(bash);
    expect(visibleWorktreeHint(null, [])).toBe(null);
  });
});

describe("shownWorktreeHint", () => {
  const abs: WorktreeHint = { path: "/x/wt", branch: "feat/wt", via: "enter" };
  const agent: WorktreeHint = { path: "/x/wt", branch: "", via: "agent", label: "x" };
  const t = (status: string, isolation: string) => ({ id: "a", status, isolation }) as never;
  test("a hint for another folder is shown", () => {
    expect(shownWorktreeHint(abs, [], "/x/main")).toBe(abs);
    expect(shownWorktreeHint(agent, [t("running", "worktree")], "/x/main")).toBe(agent);
  });
  test("null for the chat's own folder, a left worktree, or a dead agent", () => {
    expect(shownWorktreeHint(abs, [], "/x/wt")).toBe(null);
    expect(shownWorktreeHint({ ...abs, via: "exit" }, [], "/x/main")).toBe(null);
    expect(shownWorktreeHint(agent, [t("completed", "worktree")], "/x/main")).toBe(null);
    expect(shownWorktreeHint(null, [], "/x/main")).toBe(null);
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

describe("narrow side-pane bar hooks", () => {
  // The container queries themselves (chat.css, `@container chat-side-bar`)
  // cannot be asserted in happy-dom, which does no layout or container-query
  // evaluation. This pins the markup they hide: the classed spans, and the
  // `c-topbar-side` class that carries `container-type`.
  test("the pill wraps 'worktree · ' in .c-wt-word and keeps the branch name", () => {
    const c = chip(mount(<WorktreeHintChip hint={bash} currentPath="/r" />))[0]!;
    const word = c.findAll((n) => n.props.className === "c-wt-word");
    expect(word.length).toBe(1);
    expect(text(word[0]!)).toBe("worktree · ");
    expect(text(c)).toBe("worktree · feat/foo");
  });
  test("a bare Topbar wears c-topbar-side; a full-page one does not", () => {
    const props = { sessionId: "s1", running: false } as const;
    const bare = mount(<Topbar {...props} bare />);
    const full = mount(<Topbar {...props} />);
    const has = (r: ReactTestRenderer) =>
      r.root.findAll((n) => typeof n.type === "string" && String(n.props.className ?? "").includes("c-topbar-side")).length;
    expect(has(bare)).toBe(1);
    expect(has(full)).toBe(0);
  });
});
