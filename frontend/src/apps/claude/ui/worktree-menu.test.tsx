import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";
import { useState } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

const { WorktreeMenuBody } = await import("./WorktreeMenu");
const { worktreeRows } = await import("../protocol/worktrees");
import type { WorktreeList } from "../protocol/worktrees";

const LIST = (current: string, dirty = false): WorktreeList => ({
  root: "/r",
  current,
  branch_current: "main",
  worktrees: [
    { path: "/r", branch: "main", head: "a", name: "r", managed: false, is_main: true, dirty: false },
    { path: "/r/.claude/worktrees/feat", branch: "feat", head: "b", name: "feat", managed: true, is_main: false, dirty },
  ],
});

function mount(list: WorktreeList) {
  const switched: string[] = [];
  const created: string[] = [];
  let removes = 0;
  function Harness() {
    const [creating, setCreating] = useState<string | null>(null);
    const [confirming, setConfirming] = useState(false);
    return (
      <WorktreeMenuBody
        rows={worktreeRows(list)}
        onSwitch={(p) => switched.push(p)}
        creating={creating}
        onCreating={setCreating}
        onCreate={(n) => created.push(n)}
        confirming={confirming}
        onRemove={() => {
          if (confirming) removes += 1;
          else setConfirming(true);
        }}
      />
    );
  }
  let r: ReactTestRenderer | undefined;
  act(() => {
    r = create(<Harness />);
  });
  const root = r!.root;
  const buttons = () => root.findAll((n) => n.type === "button");
  const button = (text: string) => buttons().find((b) => label(b) === text)!;
  return { r: r!, root, buttons, button, switched, created, removes: () => removes };
}

describe("WorktreeMenuBody", () => {
  test("one row per worktree, the current one marked and inert", () => {
    const m = mount(LIST("/r"));
    const rows = m.root.findAll((n) => n.type === "button" && String(n.props.className).startsWith("chat-worktree-row") && n.props.title);
    expect(rows.map((b) => b.props.title)).toEqual(["/r", "/r/.claude/worktrees/feat"]);
    expect(rows.map((b) => b.props["aria-current"])).toEqual(["true", undefined]);
    expect(rows[0]!.props.onClick).toBeUndefined();
    act(() => rows[1]!.props.onClick());
    expect(m.switched).toEqual(["/r/.claude/worktrees/feat"]);
    // The main checkout is never removable.
    expect(m.buttons().some((b) => label(b) === "Remove this worktree")).toBe(false);
    act(() => m.r.unmount());
  });

  test("New worktree opens a field with the default name; Enter creates, Esc cancels", () => {
    const m = mount(LIST("/r"));
    act(() => m.button("New worktree…").props.onClick());
    const field = () => m.root.findByType("input");
    expect(field().props.value).toMatch(/^claude-\d{6}-\d{4}$/);
    act(() => field().props.onChange({ currentTarget: { value: "my-tree" } }));
    act(() => field().props.onKeyDown({ key: "Enter", preventDefault() {}, stopPropagation() {} }));
    expect(m.created).toEqual(["my-tree"]);
    // A bad name does not go.
    act(() => field().props.onChange({ currentTarget: { value: "a b" } }));
    act(() => field().props.onKeyDown({ key: "Enter", preventDefault() {}, stopPropagation() {} }));
    expect(m.created).toEqual(["my-tree"]);
    act(() => field().props.onKeyDown({ key: "Escape", preventDefault() {}, stopPropagation() {} }));
    expect(m.root.findAllByType("input")).toHaveLength(0);
    act(() => m.r.unmount());
  });

  test("remove asks first, and says so when the worktree is dirty", () => {
    const clean = mount(LIST("/r/.claude/worktrees/feat"));
    act(() => clean.button("Remove this worktree").props.onClick());
    expect(clean.removes()).toBe(0);
    act(() => clean.button("Confirm remove").props.onClick());
    expect(clean.removes()).toBe(1);
    act(() => clean.r.unmount());

    const dirty = mount(LIST("/r/.claude/worktrees/feat", true));
    act(() => dirty.button("Remove this worktree").props.onClick());
    expect(dirty.buttons().some((b) => label(b) === "Has uncommitted changes. Remove anyway")).toBe(true);
    act(() => dirty.r.unmount());
  });
});

function label(b: { props: { children?: unknown } }): string {
  return ([] as unknown[]).concat(b.props.children as unknown).filter((x) => typeof x === "string").join("");
}
